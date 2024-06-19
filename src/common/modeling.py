import functools
import logging
import os
import threading
import time
from concurrent import futures
from typing import Any, Annotated, Optional
import numpy as np

import anthropic
import langfun as lf
import openai
import pandas as pd
import pyglove as pg
import torch
from transformers import BitsAndBytesConfig, pipeline

from common import modeling_utils
from common import shared_config
from common import utils
from common.console import cyan, magenta

AVAILABLE_MODELS = pd.read_csv("common/available_models.csv", skipinitialspace=True)

_DEBUG_PRINT_LOCK = threading.Lock()
_ANTHROPIC_MODELS = [
    'claude-3-opus-20240229',
    'claude-3-sonnet-20240229',
    'claude-3-haiku-20240307',
    'claude-2.1',
    'claude-2.0',
    'claude-instant-1.2',
]


def model_full_name_to_shorthand(name: str) -> str:
    """Returns model shorthand, platform, specifier and context window of the specified model."""
    try:
        platform, specifier = name.split(':')
    except:
        raise ValueError(f'Invalid model specification "{name}". Must be in format "<PLATFORM>:<Specifier>".')

    match = (AVAILABLE_MODELS["Platform"] == platform) & (AVAILABLE_MODELS["Specifier"] == specifier)
    if not np.any(match):
        raise ValueError(f"Specified model '{name}' not available.")
    shorthand = AVAILABLE_MODELS[match]["Shorthand"].iloc[0]
    return shorthand


def model_shorthand_to_full_name(shorthand: str) -> str:
    match = AVAILABLE_MODELS["Shorthand"] == shorthand
    platform = AVAILABLE_MODELS["Platform"][match].iloc[0]
    specifier = AVAILABLE_MODELS["Specifier"][match].iloc[0]
    return f"{platform}:{specifier}"


def get_model_context_window(name: str) -> int:
    """Returns the number of tokens that fit into the context of the model at most."""
    if name not in AVAILABLE_MODELS["Shorthand"].to_list():
        name = model_full_name_to_shorthand(name)
    return int(AVAILABLE_MODELS["Context window"][AVAILABLE_MODELS["Shorthand"] == name].iloc[0])


class Usage(pg.Object):
    """Usage information per completion."""

    prompt_tokens: int
    completion_tokens: int


@lf.use_init_args(['model'])
class AnthropicModel(lf.LanguageModel):
    """Anthropic model."""

    model: pg.typing.Annotated[
        pg.typing.Enum(pg.MISSING_VALUE, _ANTHROPIC_MODELS),
        'The name of the model to use.',
    ] = 'claude-instant-1.2'
    api_key: Annotated[
        str | None,
        (
            'API key. If None, the key will be read from environment variable '
            "'ANTHROPIC_API_KEY'."
        ),
    ] = None

    def _on_bound(self) -> None:
        super()._on_bound()
        self.__dict__.pop('_api_initialized', None)

    @functools.cached_property
    def _api_initialized(self) -> bool:
        self.api_key = self.api_key or os.environ.get('ANTHROPIC_API_KEY', None)

        if not self.api_key:
            raise ValueError(
                'Please specify `api_key` during `__init__` or set environment '
                'variable `ANTHROPIC_API_KEY` with your Anthropic API key.'
            )

        return True

    @property
    def model_id(self) -> str:
        """Returns a string to identify the model."""
        return f'Anthropic({self.model})'

    def _get_request_args(
            self, options: lf.LMSamplingOptions
    ) -> dict[str, Any]:
        # Reference: https://docs.anthropic.com/claude/reference/messages_post
        args = dict(
            temperature=options.temperature,
            max_tokens=options.max_tokens,
            stream=False,
            model=self.model,
        )

        if options.top_p is not None:
            args['top_p'] = options.top_p
        if options.top_k is not None:
            args['top_k'] = options.top_k
        if options.stop:
            args['stop_sequences'] = options.stop

        return args

    def _sample(self, prompts: list[lf.Message]) -> list:
        assert self._api_initialized
        return self._complete_batch(prompts)

    def _set_logging(self) -> None:
        logger: logging.Logger = logging.getLogger('anthropic')
        httpx_logger: logging.Logger = logging.getLogger('httpx')
        logger.setLevel(logging.WARNING)
        httpx_logger.setLevel(logging.WARNING)

    def _complete_batch(
            self, prompts: list[lf.Message]
    ) -> list:
        def _anthropic_chat_completion(prompt: lf.Message):
            content = prompt.text
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                messages=[{'role': 'user', 'content': content}],
                **self._get_request_args(self.sampling_options),
            )
            model_response = response.content[0].text
            samples = [lf.LMSample(model_response, score=0.0)]
            raise NotImplementedError  # TODO: Removed due to bug, see git history

        self._set_logging()
        return lf.concurrent_execute(
            _anthropic_chat_completion,
            prompts,
            executor=self.resource_id,
            max_workers=1,
            max_attempts=self.max_attempts,
            retry_interval=self.retry_interval,
            exponential_backoff=self.exponential_backoff,
            retry_on_errors=(
                anthropic.RateLimitError,
                anthropic.APIConnectionError,
                anthropic.InternalServerError,
            ),
        )


class Model:
    """Class for storing any single language model."""

    def __init__(
            self,
            name: str,
            temperature: float = 0.01,
            max_response_len: int = 2048,
            top_k: int = 50,
            repetition_penalty: float = 1.2,
            show_responses: bool = False,
            show_prompts: bool = False,
    ) -> None:
        """Initializes a model."""
        if name in AVAILABLE_MODELS["Shorthand"].to_list():
            shorthand = name
            full_name = model_shorthand_to_full_name(shorthand)
        else:
            shorthand = model_full_name_to_shorthand(name)
            full_name = name
        self.name = shorthand
        self.temperature = temperature
        self.context_window = get_model_context_window(shorthand)  # tokens
        assert max_response_len < self.context_window
        self.max_response_len = max_response_len  # tokens
        self.max_prompt_len = self.context_window - max_response_len  # tokens
        self.top_k = top_k
        self.repetition_penalty = repetition_penalty
        self.show_responses = show_responses
        self.show_prompts = show_prompts
        self.open_source = False
        self.model = self.load(full_name)

    def load(self, model_name: str) -> lf.LanguageModel:
        """Loads a language model from string representation."""
        sampling = lf.LMSamplingOptions(
            temperature=self.temperature, max_tokens=self.max_response_len
        )

        if model_name.lower().startswith('openai:'):
            if not shared_config.openai_api_key:
                utils.maybe_print_error('No OpenAI API Key specified.')
                utils.stop_all_execution(True)

            return lf.llms.OpenAI(
                model=model_name[7:],
                api_key=shared_config.openai_api_key,
                sampling_options=sampling,
            )
        elif model_name.lower().startswith('anthropic:'):
            if not shared_config.anthropic_api_key:
                utils.maybe_print_error('No Anthropic API Key specified.')
                utils.stop_all_execution(True)

            return AnthropicModel(
                model=model_name[10:],
                api_key=shared_config.anthropic_api_key,
                sampling_options=sampling,
            )
        # Pipeline works with various out-of-the-box huggingface models 
        elif model_name.lower().startswith('huggingface:'):
            self.open_source = True
            model_name = model_name[12:]
            return pipeline(
                'text-generation',
                max_length=self.context_window,
                temperature=self.temperature,
                top_k=self.top_k,
                model=model_name,
                repetition_penalty=self.repetition_penalty,
                model_kwargs={"torch_dtype": torch.bfloat16},
                device_map="auto",
                token=shared_config.huggingface_user_access_token,
            )
        elif 'unittest' == model_name.lower():
            return lf.llms.Echo()
        else:
            raise ValueError(f'ERROR: Unsupported model type: {model_name}.')

    def generate(
            self,
            prompt: str,
            do_debug: bool = False,
            temperature: float = None,
            max_tokens: Optional[int] = None,
            max_attempts: int = 3,
            top_p=0.9,
            timeout: int = 60,
            retry_interval: int = 10,
    ) -> str:
        """Generates a response to a prompt."""
        self.model.max_attempts = 1
        self.model.retry_interval = 0
        self.model.timeout = timeout
        prompt = modeling_utils.add_format(prompt, self.model, self.name)
        gen_temp = temperature if temperature is not None else self.temperature
        response, num_attempts = '', 0

        if self.open_source:
            # Handling needs to be done case by case. Default uses meta-llama formatting.
            prompt = self.handle_prompt(prompt)
            terminators = [
                self.model.tokenizer.eos_token_id,
                self.model.tokenizer.convert_tokens_to_ids("<|eot_id|>")
            ]
            # useful for controlling the length of the generated sequences.
            self.model.tokenizer.pad_token_id = self.model.tokenizer.eos_token_id
            output = self.model(prompt,
                                eos_token_id=terminators,
                                pad_token_id=terminators[0],
                                do_sample=True,
                                temperature=gen_temp,
                                top_p=top_p,
                                )
            response = output[0]['generated_text'][len(prompt):]
        else:
            with modeling_utils.get_lf_context(gen_temp, max_tokens):
                while not response and num_attempts < max_attempts:
                    with futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(lf.LangFunc(prompt, lm=self.model, temperature=gen_temp))

                        try:
                            response = future.result(timeout=timeout).text
                        except (
                                openai.error.OpenAIError,
                                futures.TimeoutError,
                                lf.core.concurrent.RetryError,
                                anthropic.AnthropicError,
                        ) as e:
                            utils.maybe_print_error(e)
                            time.sleep(retry_interval)

                    num_attempts += 1

        if do_debug:
            with _DEBUG_PRINT_LOCK:
                if self.show_prompts:
                    print(magenta(prompt))
                if self.show_responses:
                    print(cyan(response))

        return response

    def handle_prompt(
            self,
            original_prompt: str,
            system_prompt: str = "Make sure to follow the instructions. Keep the output to the minimum."
    ) -> str:
        """
        Processes the prompt using the model's tokenizer with a specific template,
        and continues execution even if an error occurs during formatting.
        """
        original_prompt = modeling_utils.prepare_prompt(original_prompt, system_prompt, self.name)
        try:
            # Attempt to apply the chat template formatting
            formatted_prompt = self.model.tokenizer.apply_chat_template(
                original_prompt,
                tokenize=False,
                add_generation_prompt=True
            )
        except Exception as e:
            # Log the error and continue with the original prompt
            error_message = (
                f"An error occurred while formatting the prompt: {str(e)}. "
                f"Please check the model's documentation on Hugging Face for the correct prompt formatting: "
                f"https://huggingface.co/{self.model.model_name[12:]}"
            )
            print(error_message)
            # Use the original prompt if the formatting fails
            formatted_prompt = original_prompt

        # The function continues processing with either the formatted or original prompt
        return formatted_prompt

    def print_config(self) -> None:
        settings = {
            'model_name': self.name,
            'temperature': self.temperature,
            'max_response_len': self.max_response_len,
            'show_responses': self.show_responses,
            'show_prompts': self.show_prompts,
        }
        print(utils.to_readable_json(settings))


class MultimodalModel(Model):
    """Class for storing any multimodal language model."""

    def __init__(
            self,
            name: str,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = 2000,
            top_k: Optional[int] = 50,
            repetition_penalty: Optional[float] = 1.2,
            show_responses: bool = False,
            show_prompts: bool = False,
    ) -> None:
        """Initializes a multimodal model."""
        super().__init__(name, temperature, max_tokens, top_k, repetition_penalty, show_responses, show_prompts)

    def load(self, model_name: str):
        """Loads a multimodal model from string representation."""
        if model_name.lower().startswith('huggingface:'):
            model_name = model_name[12:]
            if model_name != "llava-hf/llava-1.5-7b-hf":
                print(
                    "Warning: Model output is cut according to Llava 1.5 standard input format < output[len(prompt)-5:] >.")
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16
            )
            return pipeline("image-to-text", model=model_name,
                            model_kwargs={"quantization_config": quantization_config})
        else:
            raise ValueError(f'ERROR: Unsupported model type: {model_name}.')

    def generate(
            self,
            image: torch.Tensor,
            prompt: str,
            do_debug: bool = False,
            temperature: Optional[float] = None,
            max_response_len: Optional[int] = None,
            top_k: int = 50,
            repetition_penalty: float = 1.2,
    ) -> str:
        max_response_len = max_response_len or self.max_response_len
        response = self.model(
            image,
            prompt=prompt,
            generate_kwargs={
                "max_new_tokens": max_response_len,
                "temperature": temperature or self.temperature,
                "top_k": top_k,
                "repetition_penalty": repetition_penalty
            }
        )[0]["generated_text"][
                   len(prompt) - 5:]  # Because of <image> in the Llava template. Might need adjustment for other LLMs.

        if do_debug:
            if self.show_prompts:
                print(f"Prompt: {prompt}")
            if self.show_responses:
                print(f"Response: {response}")

        return response


class FakeModel(Model):
    """Class for faking responses during unit tests."""

    def __init__(
            self,
            static_response: str = '',
            sequential_responses: Optional[list[str]] = None,
    ) -> None:
        Model.__init__(self, name='unittest')
        self.static_response = static_response
        self.sequential_responses = sequential_responses
        self.sequential_response_idx = 0

        if static_response:
            self.model = lf.llms.StaticResponse(static_response)
        elif sequential_responses:
            self.model = lf.llms.StaticSequence(sequential_responses)
        else:
            self.model = lf.llms.Echo()

    def generate(
            self,
            prompt: str,
            do_debug: bool = False,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            max_attempts: int = 1000,
            timeout: int = 60,
            retry_interval: int = 10,
    ) -> str:
        if self.static_response:
            return self.static_response
        elif self.sequential_responses:
            response = self.sequential_responses[
                self.sequential_response_idx % len(self.sequential_responses)
                ]
            self.sequential_response_idx += 1
            return response
        else:
            return ''
