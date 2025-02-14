# Copyright 2021 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, Optional, Union

import numpy as np

import requests

from ..utils import is_torch_available, logging
from .audio_utils import ffmpeg_read
from .base import ChunkPipeline


if TYPE_CHECKING:
    from pyctcdecode import BeamSearchDecoderCTC

    from ...feature_extraction_sequence_utils import SequenceFeatureExtractor

logger = logging.get_logger(__name__)

if is_torch_available():
    from ..models.auto.modeling_auto import MODEL_FOR_CTC_MAPPING, MODEL_FOR_SPEECH_SEQ_2_SEQ_MAPPING


def rescale_stride(stride, ratio):
    """
    Rescales the stride values from audio space to tokens/logits space.

    (160_000, 16_000, 16_000) -> (2000, 200, 200) for instance.
    """
    # Shape is [B, SEQ] for tokens
    # [B, SEQ, V] for logits

    new_strides = []
    for input_n, left, right in stride:
        token_n = int(round(input_n * ratio))
        left = int(round(left / input_n * token_n))
        right = int(round(right / input_n * token_n))
        new_stride = (token_n, left, right)
        new_strides.append(new_stride)

    return new_strides


def chunk_iter(inputs, feature_extractor, chunk_len, stride_left, stride_right, dtype=None):
    inputs_len = inputs.shape[0]
    step = chunk_len - stride_left - stride_right
    for i in range(0, inputs_len, step):
        # add start and end paddings to the chunk
        chunk = inputs[i : i + chunk_len]
        processed = feature_extractor(chunk, sampling_rate=feature_extractor.sampling_rate, return_tensors="pt")
        if dtype is not None:
            processed = processed.to(dtype=dtype)
        _stride_left = 0 if i == 0 else stride_left
        is_last = i + step + stride_left >= inputs_len
        _stride_right = 0 if is_last else stride_right

        if "input_features" in processed:
            processed_len = processed["input_features"].shape[-1]
        elif "input_values" in processed:
            processed_len = processed["input_values"].shape[-1]
        chunk_len = chunk.shape[0]
        stride = (chunk_len, _stride_left, _stride_right)
        if processed_len != chunk.shape[-1]:
            ratio = processed_len / chunk_len
            stride = rescale_stride([stride], ratio)[0]
        if chunk.shape[0] > _stride_left:
            yield {"is_last": is_last, "stride": stride, **processed}


def _find_longest_common_sequence(sequences, tokenizer):
    # TODO  Use a faster algorithm this can probably be done in O(n)
    # using suffix array.
    # It might be tedious to do because of fault tolerance.
    # We actually have a really good property which is that the total sequence
    # MUST be those subsequences in order.
    # Also the algorithm should be more tolerant to errors.
    sequence = [tok_id for tok_id in sequences[0][0].tolist() if tok_id not in tokenizer.all_special_ids]
    for new_seq in sequences[1:]:
        new_sequence = [tok_id for tok_id in new_seq[0].tolist() if tok_id not in tokenizer.all_special_ids]

        index = 0
        max_ = 0.0
        for i in range(1, len(new_sequence) + 1):
            # epsilon to favor long perfect matches
            eps = i / 10000.0
            matches = np.sum(np.array(sequence[-i:]) == np.array(new_sequence[:i]))
            matching = matches / i + eps
            if matches > 1 and matching > max_:
                index = i
                max_ = matching
        sequence.extend(new_sequence[index:])
    return np.array(sequence)


class AutomaticSpeechRecognitionPipeline(ChunkPipeline):
    """
    Pipeline that aims at extracting spoken text contained within some audio.

    The input can be either a raw waveform or a audio file. In case of the audio file, ffmpeg should be installed for
    to support multiple audio formats

    Example:

    ```python
    >>> from transformers import pipeline

    >>> transcriber = pipeline(model="openai/whisper-base")
    >>> transcriber("https://huggingface.co/datasets/Narsil/asr_dummy/resolve/main/1.flac")
    {'text': ' He hoped there would be stew for dinner, turnips and carrots and bruised potatoes and fat mutton pieces to be ladled out in thick, peppered flour-fatten sauce.'}
    ```

    Learn more about the basics of using a pipeline in the [pipeline tutorial](../pipeline_tutorial)

    Arguments:
        model ([`PreTrainedModel`] or [`TFPreTrainedModel`]):
            The model that will be used by the pipeline to make predictions. This needs to be a model inheriting from
            [`PreTrainedModel`] for PyTorch and [`TFPreTrainedModel`] for TensorFlow.
        tokenizer ([`PreTrainedTokenizer`]):
            The tokenizer that will be used by the pipeline to encode data for the model. This object inherits from
            [`PreTrainedTokenizer`].
        feature_extractor ([`SequenceFeatureExtractor`]):
            The feature extractor that will be used by the pipeline to encode waveform for the model.
        chunk_length_s (`float`, *optional*, defaults to 0):
            The input length for in each chunk. If `chunk_length_s = 0` then chunking is disabled (default). Only
            available for CTC models, e.g. [`Wav2Vec2ForCTC`].

            <Tip>

            For more information on how to effectively use `chunk_length_s`, please have a look at the [ASR chunking
            blog post](https://huggingface.co/blog/asr-chunking).

            </Tip>

        stride_length_s (`float`, *optional*, defaults to `chunk_length_s / 6`):
            The length of stride on the left and right of each chunk. Used only with `chunk_length_s > 0`. This enables
            the model to *see* more context and infer letters better than without this context but the pipeline
            discards the stride bits at the end to make the final reconstitution as perfect as possible.

            <Tip>

            For more information on how to effectively use `stride_length_s`, please have a look at the [ASR chunking
            blog post](https://huggingface.co/blog/asr-chunking).

            </Tip>

        framework (`str`, *optional*):
            The framework to use, either `"pt"` for PyTorch or `"tf"` for TensorFlow. The specified framework must be
            installed. If no framework is specified, will default to the one currently installed. If no framework is
            specified and both frameworks are installed, will default to the framework of the `model`, or to PyTorch if
            no model is provided.
        device (`int`, *optional*, defaults to -1):
            Device ordinal for CPU/GPU supports. Setting this to -1 will leverage CPU, a positive will run the model on
            the associated CUDA device id.
        decoder (`pyctcdecode.BeamSearchDecoderCTC`, *optional*):
            [PyCTCDecode's
            BeamSearchDecoderCTC](https://github.com/kensho-technologies/pyctcdecode/blob/2fd33dc37c4111417e08d89ccd23d28e9b308d19/pyctcdecode/decoder.py#L180)
            can be passed for language model boosted decoding. See [`Wav2Vec2ProcessorWithLM`] for more information.

    """

    def __init__(
        self,
        feature_extractor: Union["SequenceFeatureExtractor", str],
        *,
        decoder: Optional[Union["BeamSearchDecoderCTC", str]] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.feature_extractor = feature_extractor

        if self.model.__class__ in MODEL_FOR_SPEECH_SEQ_2_SEQ_MAPPING.values():
            self.type = "seq2seq"
        elif (
            feature_extractor._processor_class
            and feature_extractor._processor_class.endswith("WithLM")
            and decoder is not None
        ):
            self.decoder = decoder
            self.type = "ctc_with_lm"
        else:
            self.type = "ctc"

        if self.framework == "tf":
            raise ValueError("The AutomaticSpeechRecognitionPipeline is only available in PyTorch.")

        self.check_model_type(dict(MODEL_FOR_SPEECH_SEQ_2_SEQ_MAPPING.items() + MODEL_FOR_CTC_MAPPING.items()))

    def __call__(
        self,
        inputs: Union[np.ndarray, bytes, str],
        **kwargs,
    ):
        """
        Transcribe the audio sequence(s) given as inputs to text. See the [`AutomaticSpeechRecognitionPipeline`]
        documentation for more information.

        Args:
            inputs (`np.ndarray` or `bytes` or `str` or `dict`):
                The inputs is either :
                    - `str` that is the filename of the audio file, the file will be read at the correct sampling rate
                      to get the waveform using *ffmpeg*. This requires *ffmpeg* to be installed on the system.
                    - `bytes` it is supposed to be the content of an audio file and is interpreted by *ffmpeg* in the
                      same way.
                    - (`np.ndarray` of shape (n, ) of type `np.float32` or `np.float64`)
                        Raw audio at the correct sampling rate (no further check will be done)
                    - `dict` form can be used to pass raw audio sampled at arbitrary `sampling_rate` and let this
                      pipeline do the resampling. The dict must be in the format `{"sampling_rate": int, "raw":
                      np.array}` with optionally a `"stride": (left: int, right: int)` than can ask the pipeline to
                      treat the first `left` samples and last `right` samples to be ignored in decoding (but used at
                      inference to provide more context to the model). Only use `stride` with CTC models.
            return_timestamps (*optional*, `str`):
                Only available for pure CTC models. If set to `"char"`, the pipeline will return `timestamps` along the
                text for every character in the text. For instance if you get `[{"text": "h", "timestamps": (0.5,0.6),
                {"text": "i", "timestamps": (0.7, .9)}]`, then it means the model predicts that the letter "h" was
                pronounced after `0.5` and before `0.6` seconds. If set to `"word"`, the pipeline will return
                `timestamps` along the text for every word in the text. For instance if you get `[{"text": "hi ",
                "timestamps": (0.5,0.9), {"text": "there", "timestamps": (1.0, .1.5)}]`, then it means the model
                predicts that the word "hi" was pronounced after `0.5` and before `0.9` seconds.
            generate_kwargs (`dict`, *optional*):
                The dictionary of ad-hoc parametrization of `generate_config` to be used for the generation call. For a
                complete overview of generate, check the [following
                guide](https://huggingface.co/docs/transformers/en/main_classes/text_generation).
            max_new_tokens (`int`, *optional*):
                The maximum numbers of tokens to generate, ignoring the number of tokens in the prompt.

        Return:
            `Dict`: A dictionary with the following keys:
                - **text** (`str` ) -- The recognized text.
                - **chunks** (*optional(, `List[Dict]`)
                        When using `return_timestamps`, the `chunks` will become a list containing all the various text
                        chunks identified by the model, *e.g.* `[{"text": "hi ", "timestamps": (0.5,0.9), {"text":
                        "there", "timestamps": (1.0, 1.5)}]`. The original full text can roughly be recovered by doing
                        `"".join(chunk["text"] for chunk in output["chunks"])`.
        """
        return super().__call__(inputs, **kwargs)

    def _sanitize_parameters(
        self,
        chunk_length_s=None,
        stride_length_s=None,
        ignore_warning=None,
        decoder_kwargs=None,
        return_timestamps=None,
        generate_kwargs=None,
        max_new_tokens=None,
    ):
        # No parameters on this pipeline right now
        preprocess_params = {}
        if chunk_length_s is not None:
            preprocess_params["chunk_length_s"] = chunk_length_s
        if stride_length_s is not None:
            preprocess_params["stride_length_s"] = stride_length_s
        if ignore_warning is not None:
            preprocess_params["ignore_warning"] = ignore_warning

        forward_params = {"generate_kwargs": {}}
        if max_new_tokens is not None:
            forward_params["generate_kwargs"]["max_new_tokens"] = max_new_tokens
        if generate_kwargs is not None:
            if max_new_tokens is not None and "max_new_tokens" in generate_kwargs:
                raise ValueError(
                    "`max_new_tokens` is defined both as an argument and inside `generate_kwargs` argument, please use"
                    " only 1 version"
                )
            forward_params["generate_kwargs"].update(generate_kwargs)

        postprocess_params = {}
        if decoder_kwargs is not None:
            postprocess_params["decoder_kwargs"] = decoder_kwargs
        if return_timestamps is not None:
            postprocess_params["return_timestamps"] = return_timestamps

        return preprocess_params, forward_params, postprocess_params

    def preprocess(self, inputs, chunk_length_s=0, stride_length_s=None, ignore_warning=False):
        if isinstance(inputs, str):
            if inputs.startswith("http://") or inputs.startswith("https://"):
                # We need to actually check for a real protocol, otherwise it's impossible to use a local file
                # like http_huggingface_co.png
                inputs = requests.get(inputs).content
            else:
                with open(inputs, "rb") as f:
                    inputs = f.read()

        if isinstance(inputs, bytes):
            inputs = ffmpeg_read(inputs, self.feature_extractor.sampling_rate)

        stride = None
        extra = {}
        if isinstance(inputs, dict):
            stride = inputs.pop("stride", None)
            # Accepting `"array"` which is the key defined in `datasets` for
            # better integration
            if not ("sampling_rate" in inputs and ("raw" in inputs or "array" in inputs)):
                raise ValueError(
                    "When passing a dictionnary to AutomaticSpeechRecognitionPipeline, the dict needs to contain a "
                    '"raw" key containing the numpy array representing the audio and a "sampling_rate" key, '
                    "containing the sampling_rate associated with that array"
                )

            _inputs = inputs.pop("raw", None)
            if _inputs is None:
                _inputs = inputs.pop("array", None)
            in_sampling_rate = inputs.pop("sampling_rate")
            extra = inputs
            inputs = _inputs
            if in_sampling_rate != self.feature_extractor.sampling_rate:
                import torch
                from torchaudio import functional as F

                inputs = F.resample(
                    torch.from_numpy(inputs), in_sampling_rate, self.feature_extractor.sampling_rate
                ).numpy()
                ratio = self.feature_extractor.sampling_rate / in_sampling_rate
            else:
                ratio = 1
            if stride is not None:
                if stride[0] + stride[1] > inputs.shape[0]:
                    raise ValueError("Stride is too large for input")

                # Stride needs to get the chunk length here, it's going to get
                # swallowed by the `feature_extractor` later, and then batching
                # can add extra data in the inputs, so we need to keep track
                # of the original length in the stride so we can cut properly.
                stride = (inputs.shape[0], int(round(stride[0] * ratio)), int(round(stride[1] * ratio)))
        if not isinstance(inputs, np.ndarray):
            raise ValueError(f"We expect a numpy ndarray as input, got `{type(inputs)}`")
        if len(inputs.shape) != 1:
            raise ValueError("We expect a single channel audio input for AutomaticSpeechRecognitionPipeline")

        if chunk_length_s:
            if self.type == "seq2seq" and not ignore_warning:
                logger.warning(
                    "Using `chunk_length_s` is very experimental with seq2seq models. The results will not necessarily"
                    " be entirely accurate and will have caveats. More information:"
                    " https://github.com/huggingface/transformers/pull/20104. Ignore this warning with pipeline(...,"
                    " ignore_warning=True)"
                )
                self._preprocess_params["ignore_warning"] = True
            if stride_length_s is None:
                stride_length_s = chunk_length_s / 6

            if isinstance(stride_length_s, (int, float)):
                stride_length_s = [stride_length_s, stride_length_s]

            # XXX: Carefuly, this variable will not exist in `seq2seq` setting.
            # Currently chunking is not possible at this level for `seq2seq` so
            # it's ok.
            align_to = getattr(self.model.config, "inputs_to_logits_ratio", 1)
            chunk_len = int(round(chunk_length_s * self.feature_extractor.sampling_rate / align_to) * align_to)
            stride_left = int(round(stride_length_s[0] * self.feature_extractor.sampling_rate / align_to) * align_to)
            stride_right = int(round(stride_length_s[1] * self.feature_extractor.sampling_rate / align_to) * align_to)

            if chunk_len < stride_left + stride_right:
                raise ValueError("Chunk length must be superior to stride length")

            # make sure that
            for item in chunk_iter(
                inputs, self.feature_extractor, chunk_len, stride_left, stride_right, self.torch_dtype
            ):
                yield item
        else:
            processed = self.feature_extractor(
                inputs, sampling_rate=self.feature_extractor.sampling_rate, return_tensors="pt"
            )
            if self.torch_dtype is not None:
                processed = processed.to(dtype=self.torch_dtype)
            if stride is not None:
                if self.model.__class__ in MODEL_FOR_SPEECH_SEQ_2_SEQ_MAPPING.values():
                    raise ValueError("Stride is only usable with CTC models, try removing it")

                processed["stride"] = stride
            yield {"is_last": True, **processed, **extra}

    def _forward(self, model_inputs, generate_kwargs=None):
        if generate_kwargs is None:
            generate_kwargs = {}

        is_last = model_inputs.pop("is_last")
        if self.type == "seq2seq":
            encoder = self.model.get_encoder()
            # Consume values so we can let extra information flow freely through
            # the pipeline (important for `partial` in microphone)
            if "input_features" in model_inputs:
                inputs = model_inputs.pop("input_features")
            elif "input_values" in model_inputs:
                inputs = model_inputs.pop("input_values")
            else:
                raise ValueError(
                    "Seq2Seq speech recognition model requires either a "
                    f"`input_features` or `input_values` key, but only has {model_inputs.keys()}"
                )

            # we need to pass `processed.get("attention_mask")` here since audio encoder
            # attention mask  length is different from expected text decoder `encoder_attention_mask` length
            # `generate` magic to create the mask automatically won't work, we basically need to help
            # it here.
            attention_mask = model_inputs.pop("attention_mask", None)

            tokens = self.model.generate(
                encoder_outputs=encoder(inputs, attention_mask=attention_mask),
                attention_mask=attention_mask,
                **generate_kwargs,
            )

            out = {"tokens": tokens}

        else:
            stride = model_inputs.pop("stride", None)
            input_values = model_inputs.pop("input_values")
            attention_mask = model_inputs.pop("attention_mask", None)
            outputs = self.model(input_values=input_values, attention_mask=attention_mask)
            logits = outputs.logits

            if self.type == "ctc_with_lm":
                out = {"logits": logits}
            else:
                out = {"tokens": logits.argmax(dim=-1)}
            if stride is not None:
                # Send stride to `postprocess`.
                # it needs to be handled there where
                # the pieces are to be concatenated.
                ratio = 1 / self.model.config.inputs_to_logits_ratio
                if isinstance(stride, tuple):
                    out["stride"] = rescale_stride([stride], ratio)[0]
                else:
                    out["stride"] = rescale_stride(stride, ratio)
        # Leftover
        extra = model_inputs
        return {"is_last": is_last, **out, **extra}

    def postprocess(self, model_outputs, decoder_kwargs: Optional[Dict] = None, return_timestamps=None):
        # Optional return types
        optional = {}

        if return_timestamps and self.type == "seq2seq":
            raise ValueError("We cannot return_timestamps yet on non-ctc models !")
        if return_timestamps == "char" and self.type == "ctc_with_lm":
            raise ValueError("CTC with LM cannot return `char` timestamps, only `words`")

        final_items = []
        key = "logits" if self.type == "ctc_with_lm" else "tokens"
        stride = None
        for outputs in model_outputs:
            items = outputs[key].numpy()
            stride = outputs.pop("stride", None)
            if stride is not None and self.type in {"ctc", "ctc_with_lm"}:
                total_n, left, right = stride
                # Total_n might be < logits.shape[1]
                # because of padding, that's why
                # we need to reconstruct this information
                # This won't work with left padding (which doesn't exist right now)
                right_n = total_n - right
                items = items[:, left:right_n]
            final_items.append(items)
        if stride and self.type == "seq2seq":
            items = _find_longest_common_sequence(final_items, self.tokenizer)
        else:
            items = np.concatenate(final_items, axis=1)
            items = items.squeeze(0)
        if self.type == "ctc_with_lm":
            if decoder_kwargs is None:
                decoder_kwargs = {}
            beams = self.decoder.decode_beams(items, **decoder_kwargs)
            text = beams[0][0]
            if return_timestamps:
                # Simply cast from pyctcdecode format to wav2vec2 format to leverage
                # pre-existing code later
                chunk_offset = beams[0][2]
                word_offsets = []
                for word, (start_offset, end_offset) in chunk_offset:
                    word_offsets.append({"word": word, "start_offset": start_offset, "end_offset": end_offset})

        else:
            skip_special_tokens = self.type != "ctc"
            text = self.tokenizer.decode(items, skip_special_tokens=skip_special_tokens)
            if return_timestamps:
                char_offsets = self.tokenizer.decode(
                    items, skip_special_tokens=skip_special_tokens, output_char_offsets=True
                )["char_offsets"]
                if return_timestamps == "word":
                    word_offsets = self.tokenizer._get_word_offsets(
                        char_offsets, self.tokenizer.replace_word_delimiter_char
                    )

        if return_timestamps:
            if return_timestamps == "word":
                offsets = word_offsets
            else:
                offsets = char_offsets
            chunks = []
            for item in offsets:
                start = item["start_offset"] * self.model.config.inputs_to_logits_ratio
                start /= self.feature_extractor.sampling_rate

                stop = item["end_offset"] * self.model.config.inputs_to_logits_ratio
                stop /= self.feature_extractor.sampling_rate

                chunks.append({"text": item[return_timestamps], "timestamp": (start, stop)})
            optional["chunks"] = chunks

        extra = defaultdict(list)
        for output in model_outputs:
            output.pop("tokens", None)
            output.pop("logits", None)
            output.pop("is_last", None)
            for k, v in output.items():
                extra[k].append(v)
        return {"text": text, **optional, **extra}
