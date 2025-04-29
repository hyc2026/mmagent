# Copyright 2025 The HuggingFace Team. All rights reserved.
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

import os
import textwrap
import warnings
from collections import defaultdict
from contextlib import nullcontext
from typing import Any, Callable, Optional, Sized, Union
from qwen_omni_utils import process_mm_info
import torch
import torch.utils.data
import transformers
from accelerate.utils import broadcast_object_list, gather, gather_object, is_peft_model, set_seed
from datasets import Dataset, IterableDataset
from packaging import version
from torch import nn
from torch.utils.data import Sampler
from transformers import (
    AutoModelForCausalLM,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    GenerationConfig,
    PreTrainedModel,
    PreTrainedTokenizerBase,
    Trainer,
    TrainerCallback,
    is_wandb_available,
)
from transformers.integrations.deepspeed import is_deepspeed_zero3_enabled
from transformers.utils import is_peft_available

from ..data_utils import apply_chat_template, is_conversational, maybe_apply_chat_template
from ..extras.profiling import profiling_context, profiling_decorator
from ..extras.vllm_client import VLLMClient
from ..import_utils import is_deepspeed_available, is_rich_available, is_vllm_available
from ..models import create_reference_model, prepare_deepspeed, unwrap_model_for_generation
from .callbacks import SyncRefModelCallback
from .grpo_config import GRPOConfig
from .utils import (
    generate_model_card,
    get_comet_experiment_url,
    pad,
    print_prompt_completions_sample,
    selective_log_softmax,
)
import sys
from mmagent.retrieve import back_translate, translate, verify_qa
from mmagent.utils.chat_api import parallel_get_embedding, get_response_with_retry, generate_messages
from mmagent.utils.general import load_video_graph
import mmagent.videograph
sys.modules["videograph"] = mmagent.videograph
import re
import json
pattern = r"\[(.*)\](.*)"

def eval_answer(question, predict, ground_truth):
    if predict == "":
        return False
    response = verify_qa(question, ground_truth, predict).lower()
    return True if "yes" in response else False

def search(query, video_graph, history_clip=set(), threshold=0.05):
    model = "text-embedding-3-large"
    queries = back_translate(video_graph, [query])
    query_embedding = parallel_get_embedding(model, queries)[0]
    nodes = video_graph.search_text_nodes(query_embedding, threshold=threshold)
    nodes = sorted(nodes, key=lambda x: x[1], reverse=True)
    resp = f'Search results of query "{query}":\n\n'
    resp_len = len(resp)
    _clip = set()
    raw_data = list()
    for node in nodes:
        if len(_clip) == 5:
            break
        node_id = node[0]
        node_score = node[1]
        clip_id = video_graph.nodes[node_id].metadata['timestamp']
        if clip_id in _clip:
            continue
        _clip.add(clip_id)
        if clip_id in history_clip:
            continue
        history_clip.add(clip_id)
        clip_node_id = video_graph.text_nodes_by_clip[clip_id]
        clip_node_id = sorted(clip_node_id)
        
        content = translate(video_graph, [video_graph.nodes[_node_id].metadata['contents'][0] for _node_id in clip_node_id])
        text = '\n'.join(content)

        raw_data.append({'clip_id': 'clip_' + str(clip_id), 'memory': content})
        
        resp = resp + 'ID=' + str(clip_id) + '\n' + text + '\n\n'
    if len(resp) < resp_len + 5:
        resp = resp + 'No results found.\n\n'
    return resp, history_clip, raw_data


if is_deepspeed_available():
    import deepspeed

if is_peft_available():
    from peft import PeftConfig, get_peft_model


if is_wandb_available():
    import wandb

# What we call a reward function is a callable that takes a list of prompts and completions and returns a list of
# rewards. When it's a string, it's a model ID, so it's loaded as a pretrained model.
RewardFunc = Union[str, PreTrainedModel, Callable[[list, list], list[float]]]


class RepeatRandomSampler(Sampler):
    """
    Sampler that repeats the indices of a dataset in a structured manner.

    Args:
        data_source (`Sized`):
            Dataset to sample from.
        mini_repeat_count (`int`):
            Number of times to repeat each index per batch.
        batch_size (`int`, *optional*, defaults to `1`):
            Number of unique indices per batch.
        repeat_count (`int`, *optional*, defaults to `1`):
            Number of times to repeat the full sampling process.
        seed (`int` or `None`, *optional*, defaults to `None`):
            Random seed for reproducibility (only affects this sampler).

    Example:
    ```python
    >>> sampler = RepeatRandomSampler(["a", "b", "c", "d", "e", "f", "g"], mini_repeat_count=2, batch_size=3, repeat_count=4)
    >>> list(sampler)
    [4, 4, 3, 3, 0, 0,
     4, 4, 3, 3, 0, 0,
     4, 4, 3, 3, 0, 0,
     4, 4, 3, 3, 0, 0,

     1, 1, 2, 2, 6, 6,
     1, 1, 2, 2, 6, 6,
     1, 1, 2, 2, 6, 6,
     1, 1, 2, 2, 6, 6]
    ```

    ```txt
    mini_repeat_count = 3
          -   -   -
         [0,  0,  0,  1,  1,  1,  2,  2,  2,  3,  3,  3,      |
          4,  4,  4,  5,  5,  5,  6,  6,  6,  7,  7,  7,      |
          8,  8,  8,  9,  9,  9, 10, 10, 10, 11, 11, 11,      |
                                                                repeat_count = 2
          0,  0,  0,  1,  1,  1,  2,  2,  2,  3,  3,  3,      |
          4,  4,  4,  5,  5,  5,  6,  6,  6,  7,  7,  7,      |
          8,  8,  8,  9,  9,  9, 10, 10, 10, 11, 11, 11, ...] |
          ---------   ---------   ---------   ---------
           ---------   ---------   ---------   ---------
            ---------   ---------   ---------   ---------
                         batch_size = 12
    ```
    """

    def __init__(
        self,
        data_source: Sized,
        mini_repeat_count: int,
        batch_size: int = 1,
        repeat_count: int = 1,
        seed: Optional[int] = None,
    ):
        self.data_source = data_source
        self.mini_repeat_count = mini_repeat_count
        self.batch_size = batch_size
        self.repeat_count = repeat_count
        self.num_samples = len(data_source)
        self.seed = seed
        self.generator = torch.Generator()  # Create a local random generator
        if seed is not None:
            self.generator.manual_seed(seed)

    def __iter__(self):
        # E.g., [2, 4, 3, 1, 0, 6, 5] (num_samples = 7)
        indexes = torch.randperm(self.num_samples, generator=self.generator).tolist()

        #    [2, 4, 3, 1, 0, 6, 5]
        # -> [[2, 4, 3], [1, 0, 6], [5]]  (batch_size = 3)
        indexes = [indexes[i : i + self.batch_size] for i in range(0, len(indexes), self.batch_size)]

        #    [[2, 4, 3], [1, 0, 6], [5]]
        # -> [[2, 4, 3], [1, 0, 6]]
        indexes = [chunk for chunk in indexes if len(chunk) == self.batch_size]

        for chunk in indexes:
            for _ in range(self.repeat_count):
                for index in chunk:
                    for _ in range(self.mini_repeat_count):
                        yield index

    def __len__(self) -> int:
        return self.num_samples * self.mini_repeat_count * self.repeat_count


class GRPOTrainer(Trainer):
    """
    Trainer for the Group Relative Policy Optimization (GRPO) method. This algorithm was initially proposed in the
    paper [DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models](https://huggingface.co/papers/2402.03300).

    Example:

    ```python
    from datasets import load_dataset
    from trl import GRPOTrainer

    dataset = load_dataset("trl-lib/tldr", split="train")

    def reward_func(completions, **kwargs):
        # Dummy reward function that rewards completions with more unique letters.
        return [float(len(set(completion))) for completion in completions]

    trainer = GRPOTrainer(
        model="Qwen/Qwen2-0.5B-Instruct",
        reward_funcs=reward_func,
        train_dataset=dataset,
    )

    trainer.train()
    ```

    Args:
        model (`Union[str, PreTrainedModel]`):
            Model to be trained. Can be either:

            - A string, being the *model id* of a pretrained model hosted inside a model repo on huggingface.co, or
              a path to a *directory* containing model weights saved using
              [`~transformers.PreTrainedModel.save_pretrained`], e.g., `'./my_model_directory/'`. The model is
              loaded using [`~transformers.AutoModelForCausalLM.from_pretrained`] with the keywork arguments
              in `args.model_init_kwargs`.
            - A [`~transformers.PreTrainedModel`] object. Only causal language models are supported.
        reward_funcs (`Union[RewardFunc, list[RewardFunc]]`):
            Reward functions to be used for computing the rewards. To compute the rewards, we call all the reward
            functions with the prompts and completions and sum the rewards. Can be either:

            - A single reward function, such as:
                - A string: The *model ID* of a pretrained model hosted inside a model repo on huggingface.co, or a
                path to a *directory* containing model weights saved using
                [`~transformers.PreTrainedModel.save_pretrained`], e.g., `'./my_model_directory/'`. The model is loaded
                using [`~transformers.AutoModelForSequenceClassification.from_pretrained`] with `num_labels=1` and the
                keyword arguments in `args.model_init_kwargs`.
                - A [`~transformers.PreTrainedModel`] object: Only sequence classification models are supported.
                - A custom reward function: The function is provided with the prompts and the generated completions,
                  plus any additional columns in the dataset. It should return a list of rewards. Custom reward
                  functions can also return None when the reward is not applicable to those samples. This is useful for
                  multi-task training where different reward functions apply to different types of samples. When a
                  reward function returns None for a sample, that reward function is excluded from the reward
                  calculation for that sample. For more details, see
                  [Using a custom reward function](#using-a-custom-reward-function).
            - A list of reward functions, where each item can independently be any of the above types. Mixing different
            types within the list (e.g., a string model ID and a custom reward function) is allowed.
        args ([`GRPOConfig`], *optional*, defaults to `None`):
            Configuration for this trainer. If `None`, a default configuration is used.
        train_dataset ([`~datasets.Dataset`] or [`~datasets.IterableDataset`]):
            Dataset to use for training. It must include a column `"prompt"`. Any additional columns in the dataset is
            ignored. The format of the samples can be either:

            - [Standard](dataset_formats#standard): Each sample contains plain text.
            - [Conversational](dataset_formats#conversational): Each sample contains structured messages (e.g., role
              and content).
        eval_dataset ([`~datasets.Dataset`], [`~datasets.IterableDataset`] or `dict[str, Union[Dataset, IterableDataset]]`):
            Dataset to use for evaluation. It must meet the same requirements as `train_dataset`.
        processing_class ([`~transformers.PreTrainedTokenizerBase`], *optional*, defaults to `None`):
            Processing class used to process the data. The padding side must be set to "left". If `None`, the
            processing class is loaded from the model's name with [`~transformers.AutoTokenizer.from_pretrained`].
        reward_processing_classes (`Union[PreTrainedTokenizerBase, list[PreTrainedTokenizerBase]]`, *optional*, defaults to `None`):
            Processing classes corresponding to the reward functions specified in `reward_funcs`. Can be either:

            - A single processing class: Used when `reward_funcs` contains only one reward function.
            - A list of processing classes: Must match the order and length of the reward functions in `reward_funcs`.
            If set to `None`, or if an element of the list corresponding to a [`~transformers.PreTrainedModel`] is
            `None`, the tokenizer for the model is automatically loaded using [`~transformers.AutoTokenizer.from_pretrained`].
            For elements in `reward_funcs` that are custom reward functions (not [`~transformers.PreTrainedModel`]),
            the corresponding entries in `reward_processing_classes` are ignored.
        callbacks (list of [`~transformers.TrainerCallback`], *optional*, defaults to `None`):
            List of callbacks to customize the training loop. Will add those to the list of default callbacks
            detailed in [here](https://huggingface.co/docs/transformers/main_classes/callback).

            If you want to remove one of the default callbacks used, use the [`~transformers.Trainer.remove_callback`]
            method.
        optimizers (`tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LambdaLR]`, *optional*, defaults to `(None, None)`):
            A tuple containing the optimizer and the scheduler to use. Will default to an instance of [`AdamW`] on your
            model and a scheduler given by [`get_linear_schedule_with_warmup`] controlled by `args`.
        peft_config ([`~peft.PeftConfig`], *optional*, defaults to `None`):
            PEFT configuration used to wrap the model. If `None`, the model is not wrapped.
    """

    _tag_names = ["trl", "grpo"]

    def __init__(
        self,
        model: Union[str, PreTrainedModel],
        ref_model: Union[str, PreTrainedModel],
        reward_funcs: Union[RewardFunc, list[RewardFunc]] = None,
        args: Optional[GRPOConfig] = None,
        train_dataset: Optional[Union[Dataset, IterableDataset]] = None,
        eval_dataset: Optional[Union[Dataset, IterableDataset, dict[str, Union[Dataset, IterableDataset]]]] = None,
        processing_class: Optional[PreTrainedTokenizerBase] = None,
        reward_processing_classes: Optional[Union[PreTrainedTokenizerBase, list[PreTrainedTokenizerBase]]] = None,
        callbacks: Optional[list[TrainerCallback]] = None,
        optimizers: tuple[Optional[torch.optim.Optimizer], Optional[torch.optim.lr_scheduler.LambdaLR]] = (None, None),
        peft_config: Optional["PeftConfig"] = None,
    ):
        # Args
        if args is None:
            model_name = model if isinstance(model, str) else model.config._name_or_path
            model_name = model_name.split("/")[-1]
            args = GRPOConfig(f"{model_name}-GRPO")

        # Models
        # Trained model
        model_init_kwargs = args.model_init_kwargs or {}

        if peft_config is not None:
            if not is_peft_available():
                raise ImportError("PEFT is required to use `peft_config`. Run `pip install peft`.")
            model = get_peft_model(model, peft_config)

        # Enable gradient checkpointing if requested
        if args.gradient_checkpointing:
            model = self._enable_gradient_checkpointing(model, args)

        # Reference model
        self.beta = args.beta
        if self.beta == 0.0:
            # If beta is 0.0, the reference model is not needed
            self.ref_model = None
        elif is_deepspeed_zero3_enabled():
            self.ref_model = ref_model
        elif is_peft_model(model):
            # If PEFT is used, the reference model is not needed since the adapter can be disabled
            # to revert to the initial model.
            self.ref_model = None
        else:
            # If PEFT configuration is not provided, create a reference model based on the initial model.
            self.ref_model = create_reference_model(model)

        # Processing class
        if processing_class is None:
            processing_class = AutoTokenizer.from_pretrained(model.config._name_or_path, padding_side="left")

        # Reward functions
        if not isinstance(reward_funcs, list):
            reward_funcs = [reward_funcs]
        for i, reward_func in enumerate(reward_funcs):
            if isinstance(reward_func, str):
                reward_funcs[i] = AutoModelForSequenceClassification.from_pretrained(
                    reward_func, num_labels=1, **model_init_kwargs
                )
        self.reward_funcs = reward_funcs

        # Reward weights
        if args.reward_weights is not None:
            if len(args.reward_weights) != len(reward_funcs):
                raise ValueError(
                    f"Number of reward weights ({len(args.reward_weights)}) must match number of reward "
                    f"functions ({len(reward_funcs)})"
                )
            self.reward_weights = torch.tensor(args.reward_weights, dtype=torch.float32)
        else:
            self.reward_weights = torch.ones(len(reward_funcs), dtype=torch.float32)

        # Reward processing class
        if reward_processing_classes is None:
            reward_processing_classes = [None] * len(reward_funcs)
        elif not isinstance(reward_processing_classes, list):
            reward_processing_classes = [reward_processing_classes]
        else:
            if len(reward_processing_classes) != len(reward_funcs):
                raise ValueError("The number of reward processing classes must match the number of reward functions.")

        for i, (reward_processing_class, reward_func) in enumerate(zip(reward_processing_classes, reward_funcs)):
            if isinstance(reward_func, PreTrainedModel):
                if reward_processing_class is None:
                    reward_processing_class = AutoTokenizer.from_pretrained(reward_func.config._name_or_path)
                if reward_processing_class.pad_token_id is None:
                    reward_processing_class.pad_token = reward_processing_class.eos_token
                # The reward model computes the reward for the latest non-padded token in the input sequence.
                # So it's important to set the pad token ID to the padding token ID of the processing class.
                reward_func.config.pad_token_id = reward_processing_class.pad_token_id
                reward_processing_classes[i] = reward_processing_class
        self.reward_processing_classes = reward_processing_classes

        # Data collator
        def data_collator(features):  # No data collation is needed in GRPO
            return features

        # Training arguments
        self.max_prompt_length = args.max_prompt_length
        self.max_completion_length = args.max_completion_length  # = |o_i| in the GRPO paper
        self.num_generations = args.num_generations  # = G in the GRPO paper
        self.temperature = args.temperature
        self.top_p = args.top_p
        self.top_k = args.top_k
        self.min_p = args.min_p
        self.repetition_penalty = args.repetition_penalty
        self.use_vllm = args.use_vllm

        # Multi-step
        self.num_iterations = args.num_iterations  # = 𝜇 in the GRPO paper
        self.epsilon_low = args.epsilon
        self.epsilon_high = args.epsilon_high if args.epsilon_high is not None else args.epsilon
        # Tracks the number of iterations (forward + backward passes), including those within a grad accum cycle
        self._step = 0
        # Buffer the batch to reuse generated outputs across multiple updates. For more details, see
        # `_get_train_sampler` and `_prepare_inputs`.
        self._buffered_inputs = [None] * args.gradient_accumulation_steps

        # The trainer estimates the number of FLOPs (floating-point operations) using the number of elements in the
        # input tensor associated with the key "input_ids". However, in GRPO, the sampled data does not include the
        # "input_ids" key. Instead, the available keys is "prompt". As a result, the trainer issues the warning:
        # "Could not estimate the number of tokens of the input, floating-point operations will not be computed." To
        # suppress this warning, we set the "estimate_tokens" key in the model's "warnings_issued" dictionary to True.
        # This acts as a flag to indicate that the warning has already been issued.
        model.warnings_issued["estimate_tokens"] = True

        # Initialize the metrics
        self._metrics = {"train": defaultdict(list), "eval": defaultdict(list)}
        self._total_train_tokens = 0
        self.log_completions = args.log_completions

        super().__init__(
            model=model,
            args=args,
            data_collator=data_collator,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            processing_class=processing_class,
            callbacks=callbacks,
            optimizers=optimizers,
        )

        # Check if the per_device_train/eval_batch_size * num processes can be divided by the number of generations
        num_processes = self.accelerator.num_processes
        global_batch_size = args.per_device_train_batch_size * num_processes
        possible_values = [n_gen for n_gen in range(2, global_batch_size + 1) if (global_batch_size) % n_gen == 0]
        if self.num_generations not in possible_values:
            raise ValueError(
                f"The global train batch size ({num_processes} x {args.per_device_train_batch_size}) must be evenly "
                f"divisible by the number of generations per prompt ({self.num_generations}). Given the current train "
                f"batch size, the valid values for the number of generations are: {possible_values}."
            )
        if self.args.eval_strategy != "no":
            global_batch_size = args.per_device_eval_batch_size * num_processes
            possible_values = [n_gen for n_gen in range(2, global_batch_size + 1) if (global_batch_size) % n_gen == 0]
            if self.num_generations not in possible_values:
                raise ValueError(
                    f"The global eval batch size ({num_processes} x {args.per_device_eval_batch_size}) must be evenly "
                    f"divisible by the number of generations per prompt ({self.num_generations}). Given the current "
                    f"eval batch size, the valid values for the number of generations are: {possible_values}."
                )

        # Ensure each process receives a unique seed to prevent duplicate completions when generating with
        # transformers if num_generations exceeds per_device_train_batch_size. We could skip it if we use vLLM, but
        # it's safer to set it in all cases.
        set_seed(args.seed, device_specific=True)

        if self.use_vllm:
            if not is_vllm_available():
                raise ImportError(
                    "vLLM is not available and `use_vllm` is set to True. Please install vLLM with "
                    "`pip install vllm` to use it."
                )

            if self.accelerator.is_main_process:
                self.vllm_client = VLLMClient(
                    args.vllm_server_host, args.vllm_server_port, connection_timeout=args.vllm_server_timeout
                )

            # vLLM specific sampling arguments
            self.guided_decoding_regex = args.vllm_guided_decoding_regex

            self._last_loaded_step = 0  # tag to avoid useless loading during grad accumulation

            # When using vLLM, the main process is responsible for loading the model weights. This can cause process
            # desynchronization and seems to lead to DeepSpeed hanging during initialization. To prevent this, we
            # synchronize all processes after vLLM has been fully initialized.
            self.accelerator.wait_for_everyone()
        else:
            self.generation_config = GenerationConfig(
                max_new_tokens=self.max_completion_length,
                do_sample=True,
                pad_token_id=processing_class.pad_token_id,
                bos_token_id=processing_class.bos_token_id,
                eos_token_id=processing_class.eos_token_id,
                temperature=self.temperature,
                top_p=self.top_p,
                top_k=self.top_k,
                min_p=self.min_p,
                repetition_penalty=self.repetition_penalty,
                cache_implementation=args.cache_implementation,
            )

        # Gradient accumulation requires scaled loss. Normally, loss scaling in the parent class depends on whether the
        # model accepts loss-related kwargs. Since we compute our own loss, this check is irrelevant. We set
        # self.model_accepts_loss_kwargs to False to enable scaling.
        self.model_accepts_loss_kwargs = False

        # Add tags to the model
        self.model.add_model_tags(self._tag_names)

        if self.ref_model is not None:
            if self.is_deepspeed_enabled:
                self.ref_model = prepare_deepspeed(self.ref_model, self.accelerator)
            else:
                self.ref_model = self.accelerator.prepare_model(self.ref_model, evaluation_mode=True)

        if args.sync_ref_model:
            self.add_callback(SyncRefModelCallback(ref_model=self.ref_model, accelerator=self.accelerator))

        for i, reward_func in enumerate(self.reward_funcs):
            if isinstance(reward_func, PreTrainedModel):
                self.reward_funcs[i] = self.accelerator.prepare_model(reward_func, evaluation_mode=True)

    def _set_signature_columns_if_needed(self):
        # If `self.args.remove_unused_columns` is True, non-signature columns are removed.
        # By default, this method sets `self._signature_columns` to the model's expected inputs.
        # In GRPOTrainer, we preprocess data, so using the model's signature columns doesn't work.
        # Instead, we set them to the columns expected by the `training_step` method, hence the override.
        if self._signature_columns is None:
            self._signature_columns = ["prompt"]

    def _get_train_sampler(self) -> Sampler:
        # Returns a sampler that
        # 1. ensures each prompt is repeated across multiple processes. This guarantees that identical prompts are
        #    distributed to different GPUs, allowing rewards to be computed and normalized correctly within each prompt
        #    group. Using the same seed across processes ensures consistent prompt assignment, preventing discrepancies
        #    in group formation.
        # 2. repeats the batch multiple times to allow reusing generations across multiple updates. Refer to
        #    _prepare_inputs to see how the generations are stored and reused.

        # In the following figure, the values are the prompt indices. The first row shows the first sampled batch, the
        # second row shows the second sampled batch, and so on.
        #
        #                                     |     GPU 0     |     GPU 1     |     GPU 2    |
        #
        #               global_step   step     <───────>  num_generations=3
        #                                      <───────────> per_device_train_batch_size=4
        #                ▲   0          0      0   0   0   1   1   1   2   2   2   3   3   3  │
        #  grad_accum=3  │   0          1      4   4   4   5   5   5   6   6   6   7   7   7  │ Generate completions for each prompt
        #                ▼   0          2      8   8   8   9   9   9  10  10  10  11  11  11  │
        #
        #                    1          3      0   0   0   1   1   1   2   2   2   3   3   3  │ The sampled prompts are the same as in the first iteration
        #                    1          4      4   4   4   5   5   5   6   6   6   7   7   7  │ Reuse the completions (here, once, because num_iterations=2)
        #                    1          5      8   8   8   9   9   9  10  10  10  11  11  11  │
        #
        #                    2          6     12  12  12  13  13  13  14  14  14  15  15  15
        #                    2          7     16  16  16  17  17  17  18  18  18  19  19  19
        #                    2          8     20  20  20  21  21  21  22  22  22  23  23  23
        #                                          ...
        effective_batch_size = (
            self.args.per_device_train_batch_size
            * self.accelerator.num_processes
            * self.args.gradient_accumulation_steps
        )
        return RepeatRandomSampler(
            data_source=self.train_dataset,
            mini_repeat_count=self.num_generations,
            batch_size=effective_batch_size // self.num_generations,
            repeat_count=self.num_iterations,
            seed=self.args.seed,
        )

    def _get_eval_sampler(self, eval_dataset) -> Sampler:
        # See _get_train_sampler for an explanation of the sampler.
        return RepeatRandomSampler(
            data_source=eval_dataset,
            mini_repeat_count=self.num_generations,
            seed=self.args.seed,
        )

    def _enable_gradient_checkpointing(self, model: PreTrainedModel, args: GRPOConfig) -> PreTrainedModel:
        """Enables gradient checkpointing for the model."""
        # Ensure use_cache is disabled
        model.config.use_cache = False

        # Enable gradient checkpointing on the base model for PEFT
        if is_peft_model(model):
            model.base_model.gradient_checkpointing_enable()
        # Enable gradient checkpointing for non-PEFT models
        else:
            model.gradient_checkpointing_enable()

        gradient_checkpointing_kwargs = args.gradient_checkpointing_kwargs or {}
        use_reentrant = (
            "use_reentrant" not in gradient_checkpointing_kwargs or gradient_checkpointing_kwargs["use_reentrant"]
        )

        if use_reentrant:
            model.enable_input_require_grads()

        return model

    # Get the per-token log probabilities for the completions for the model and the reference model
    @profiling_decorator
    def _get_per_token_logps(self, model, input_ids, inputs_embeds, attention_mask, position_ids, logits_to_keep):
        # We add 1 to `logits_to_keep` because the last logits of the sequence is later excluded
        logits = model(inputs_embeds=inputs_embeds, attention_mask=attention_mask, position_ids=position_ids).logits
        logits = logits[:, :-1, :]  # (B, L-1, V), exclude the last logit: it corresponds to the next token pred

        input_ids = input_ids[:, -logits_to_keep:]
        # For transformers<=4.48, logits_to_keep argument isn't supported, so here we drop logits ourselves.
        # See https://github.com/huggingface/trl/issues/2770
        logits = logits[:, -logits_to_keep:]
        # Divide logits by sampling temperature.
        # See https://huggingface.co/blog/the_n_implementation_details_of_rlhf_with_ppo#policy-training-implementation-details
        logits = logits / self.temperature
        return selective_log_softmax(logits, input_ids)  # compute logprobs for the input tokens

    @profiling_decorator
    def _move_model_to_vllm(self):
        # For DeepSpeed ZeRO-3, we need to gather all parameters before operations
        deepspeed_plugin = self.accelerator.state.deepspeed_plugin
        zero_stage_3 = deepspeed_plugin is not None and deepspeed_plugin.zero_stage == 3
        gather_if_zero3 = deepspeed.zero.GatheredParameters if zero_stage_3 else nullcontext

        if is_peft_model(self.model):
            # With PEFT and DeepSpeed ZeRO Stage 3, we must gather the full model at once before merging, as merging
            # adapters in a sharded manner is not supported.
            with gather_if_zero3(list(self.model.parameters())):
                self.model.merge_adapter()

                # Update vLLM weights while parameters are gathered
                for name, param in self.model.named_parameters():
                    # When using PEFT, we need to recover the original parameter name and discard some parameters
                    name = name.removeprefix("base_model.model.").replace(".base_layer", "")
                    if self.model.prefix in name:
                        continue
                    # When module to save, remove its prefix and discard the original module
                    if "original_module" in name:
                        continue
                    name = name.replace("modules_to_save.default.", "")

                    if self.accelerator.is_main_process:
                        self.vllm_client.update_named_param(name, param.data)

                # Unmerge adapters while parameters are still gathered
                self.model.unmerge_adapter()
                # Parameters will automatically be repartitioned when exiting the context
        else:
            # For non-PEFT models, simply gather and update each parameter individually.
            for name, param in self.model.named_parameters():
                with gather_if_zero3([param]):
                    if self.accelerator.is_main_process:
                        self.vllm_client.update_named_param(name, param.data)

        # Reset cache on main process
        if self.accelerator.is_main_process:
            self.vllm_client.reset_prefix_cache()

    @profiling_decorator
    def _prepare_inputs(self, inputs: dict[str, Union[torch.Tensor, Any]]) -> dict[str, Union[torch.Tensor, Any]]:
        mode = "eval" if self.control.should_evaluate else "train"
        # if int(os.environ.get('LOCAL_RANK', 0)) == 0:
        #     print(self.num_iterations) # 1
        #     print(self.args.gradient_accumulation_steps) # 1
        # exit()
        if mode == "train":
            if self.state.global_step % self.num_iterations == 0:
                inputs = self._generate_and_score_completions(inputs)
                self._buffered_inputs[self._step % self.args.gradient_accumulation_steps] = inputs
            else:
                inputs = self._buffered_inputs[self._step % self.args.gradient_accumulation_steps]
            self._step += 1
        else:
            # In evaluation, we don't reuse completions across multiple updates, so we don't need to buffer inputs.
            inputs = self._generate_and_score_completions(inputs)
        return inputs

    def _generate_and_score_completions(
        self, inputs: dict[str, Union[torch.Tensor, Any]]
    ) -> dict[str, Union[torch.Tensor, Any]]:
        with torch.no_grad():
            device = self.accelerator.device
            self.model.audio_tower = self.model.audio_tower.to(device)
            self.model.visual = self.model.visual.to(device)
            conversations, answers, memories, actions, predict_answers, history_clips, complete, has_answer = [], [], [], [], [], [], [], []
            for i in inputs:
                conversations.append(i["messages"])
                answers.append(i["answer"])
                memories.append(i["mem"])
                actions.append(0)
                predict_answers.append("")
                complete.append("")
                history_clips.append(set())
                has_answer.append(False)
            for i in range(self.args.search_rounds):
                add_generation_prompt = True if i == 0 else False
                try:
                    text = self.processing_class.apply_chat_template(conversations, add_generation_prompt=add_generation_prompt, tokenize=False)
                    audios, images, videos = process_mm_info(conversations, use_audio_in_video=True)
                    inputs = self.processing_class(text=text, audios=audios, images=images, videos=videos, return_tensors="pt", padding=True, use_audio_in_video=True)
                except:
                    print("tokenize error!!!!")
                    print(conversations)
                    print(text)
                    exit()
                if i == 0:
                    prompt_length = inputs["input_ids"].shape[1]
                with unwrap_model_for_generation(
                    self.model, self.accelerator, gather_deepspeed3_params=self.args.ds3_gather_for_generation
                ) as unwrapped_model:
                    inputs = inputs.to(unwrapped_model.device).to(unwrapped_model.dtype)
                    text_ids = unwrapped_model.generate(**inputs, generation_config=self.generation_config, use_audio_in_video=True)
                    output = self.processing_class.batch_decode(text_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
                    for idx in range(len(output)):
                        if len(conversations[idx]) == 1:
                            conversations[idx].append({
                                "role": "assistant",
                                "content": ""
                            })
                        cur_output = "<think>" + output[idx].split("<think>")[-1]
                        match_result = re.search(pattern, output[idx].split("</think>")[-1], re.DOTALL)
                        if match_result:
                            action = match_result.group(1)
                            action_content = match_result.group(2)
                        else:
                            action = "Search"
                            action_content = output[idx].split("</think>")[-1]
                        cur_output += "<|im_start|>"

                        if action == "Answer":
                            has_answer[idx] = True
                            if i < self.args.search_rounds - 1:
                                if complete[idx] == "":
                                    complete[idx] = cur_output
                                    predict_answers[idx] = action_content
                            else:
                                if complete[idx] == "":
                                    conversations[idx][1]["content"] += cur_output
                                else:
                                    conversations[idx][1]["content"] += complete[idx]
                        else:
                            if complete[idx] != "":
                                if i == self.args.search_rounds - 1:
                                    conversations[idx][1]["content"] += cur_output
                                continue
                            _, history_clips[idx], raw_data = search(action_content, load_video_graph(memories[idx]), history_clips[idx])
                            conversations[idx][1]["content"] += cur_output + "Searched knowledge:\n" + json.dumps(raw_data, ensure_ascii=False).encode("utf-8", "ignore").decode("utf-8") + "\n"
                            actions[idx] += 1
            del cur_output, complete, history_clips

            # Here are some operational tricks (num_generations >= per_device_train_batch_size)
            # Because all inputs are the same, simply pad right at the end to align the inputs
            text = self.processing_class.apply_chat_template(conversations, add_generation_prompt=False, tokenize=False)
            audios, images, videos = process_mm_info(conversations, use_audio_in_video=True)
            inputs = self.processing_class(text=text, audios=audios, images=images, videos=videos, return_tensors="pt", padding=True, use_audio_in_video=True, padding_side="right")
            with unwrap_model_for_generation(
                self.model, self.accelerator, gather_deepspeed3_params=self.args.ds3_gather_for_generation
            ) as unwrapped_model:
                inputs = inputs.to(unwrapped_model.device).to(unwrapped_model.dtype)
                input_ids = inputs["input_ids"]
                attention_masks = inputs["attention_mask"]
                pixel_values_videos = inputs["pixel_values_videos"]
                video_grid_thw = inputs["video_grid_thw"]
                video_second_per_grid = inputs["video_second_per_grid"]
                feature_attention_mask = inputs["feature_attention_mask"]
                input_features = inputs["input_features"]

                audio_feature_lengths = torch.sum(feature_attention_mask, dim=1)
                input_features = input_features.permute(0, 2, 1)[feature_attention_mask.bool()].permute(1, 0)
                position_ids, _ = unwrapped_model.get_rope_index(
                    input_ids,
                    None,
                    video_grid_thw,
                    attention_masks,
                    True,
                    audio_feature_lengths,
                    video_second_per_grid,
                )

                inputs_embeds = unwrapped_model.get_input_embeddings()(input_ids)

                # 2. Merge text , audios , image and video
                if input_ids.shape[1] != 1:
                    if input_features is not None:
                        audio_feat_lengths, audio_output_lengths = unwrapped_model.audio_tower._get_feat_extract_output_lengths(
                            audio_feature_lengths if audio_feature_lengths is not None else feature_attention_mask.sum(-1)
                        )
                        feature_lens = (
                            audio_feature_lengths if audio_feature_lengths is not None else feature_attention_mask.sum(-1)
                        )
                        audio_outputs = unwrapped_model.audio_tower(
                            input_features,
                            feature_lens=feature_lens,
                            aftercnn_lens=audio_feat_lengths,
                        )
                        audio_features = audio_outputs.last_hidden_state
                        if audio_features.shape[0] != sum(audio_output_lengths.tolist()):
                            raise ValueError("length of audio_features should match audio_output_lengths")
                        audio_mask = (input_ids == unwrapped_model.config.audio_token_index).unsqueeze(-1).expand_as(inputs_embeds)
                        audio_features = audio_features.to(inputs_embeds.device, inputs_embeds.dtype)
                        inputs_embeds = inputs_embeds.masked_scatter(audio_mask, audio_features)

                    if pixel_values_videos is not None:
                        pixel_values_videos = pixel_values_videos.type(unwrapped_model.visual.get_dtype())
                        video_embeds = unwrapped_model.visual(pixel_values_videos, grid_thw=video_grid_thw)
                        video_mask = (input_ids == unwrapped_model.config.video_token_index).unsqueeze(-1).expand_as(inputs_embeds)
                        video_embeds = video_embeds.to(inputs_embeds.device, inputs_embeds.dtype)
                        inputs_embeds = inputs_embeds.masked_scatter(video_mask, video_embeds)

                    if attention_masks is not None:
                        attention_masks = attention_masks.to(inputs_embeds.device)

            del pixel_values_videos, video_grid_thw, video_second_per_grid

            query_responses = inputs_embeds.to(device)
            query_responses = query_responses[:, -self.args.max_tokens:]
            attention_masks = attention_masks.to(device)
            attention_masks = attention_masks[:, -self.args.max_tokens:]
            position_ids = position_ids.to(device)
            position_ids = position_ids[:, :, -self.args.max_tokens:]
            responses = input_ids.to(device)
            responses = responses[:, -self.args.max_tokens:]
            logits_to_keep = responses.size(1) - prompt_length
            response_masks = torch.zeros_like(responses[:, -logits_to_keep:], dtype=torch.bool, device=device)
            # mask the model output
            for i in range(responses.shape[0]):
                mask = False
                for j in range(prompt_length, responses.shape[1]):
                    if responses[i][j] == 13708 and responses[i][j + 1] == 766:
                        mask = True
                        response_masks[i][j - prompt_length] = mask
                    elif responses[i][j] == 151644:
                        response_masks[i][j - prompt_length] = mask
                        mask = False
                    else:
                        response_masks[i][j - prompt_length] = mask

        #         # Compute prompt length and extract completion ids
        #         prompt_length = prompt_ids.size(1)
        #         prompt_ids = prompt_completion_ids[:, :prompt_length]
        #         completion_ids = prompt_completion_ids[:, prompt_length:]

        #     # Mask everything after the first EOS token
        #     is_eos = completion_ids == self.processing_class.eos_token_id
        #     eos_idx = torch.full((is_eos.size(0),), is_eos.size(1), dtype=torch.long, device=device)
        #     eos_idx[is_eos.any(dim=1)] = is_eos.int().argmax(dim=1)[is_eos.any(dim=1)]
        #     sequence_indices = torch.arange(is_eos.size(1), device=device).expand(is_eos.size(0), -1)
        #     completion_mask = (sequence_indices <= eos_idx.unsqueeze(1)).int()

        #     # Concatenate prompt_mask with completion_mask for logit computation
        #     attention_mask = torch.cat([prompt_mask, completion_mask], dim=1)  # (B, P+C)

            # we only need to compute the logits for the completion tokens

            # with torch.no_grad():
                # When using num_iterations == 1, old_per_token_logps == per_token_logps, so we can skip it's
                # computation here, and use per_token_logps.detach() instead.
            if self.num_iterations > 1:
                old_per_token_logps = self._get_per_token_logps(
                    self.model, responses, query_responses, attention_masks, position_ids, logits_to_keep
                )
            else:
                old_per_token_logps = None

            if self.beta == 0.0:
                ref_per_token_logps = None
            elif self.ref_model is not None:
                self.ref_model = self.ref_model.to(device)
                ref_per_token_logps = self._get_per_token_logps(
                    self.ref_model, responses, query_responses, attention_masks, position_ids, logits_to_keep
                )
                self.ref_model = self.ref_model.cpu()
            else:
                with self.accelerator.unwrap_model(self.model).disable_adapter():
                    ref_per_token_logps = self._get_per_token_logps(
                        self.model, responses, query_responses, attention_masks, position_ids, logits_to_keep
                    )

        #     # Decode the generated completions
        #     completions_text = self.processing_class.batch_decode(completion_ids, skip_special_tokens=True)
        #     if is_conversational(inputs[0]):
        #         completions = []
        #         for prompt, completion in zip(prompts, completions_text):
        #             bootstrap = prompt.pop()["content"] if prompt[-1]["role"] == "assistant" else ""
        #             completions.append([{"role": "assistant", "content": bootstrap + completion}])
        #     else:
        #         completions = completions_text
            rewards_per_func = torch.zeros(len(predict_answers), 1, device=device)
            for i in range(len(predict_answers)):
                predict_answer = predict_answers[i]
                ground_truth_answer = answers[i]
                question = conversations[i][0]["content"][-1]["text"]
                score = eval_answer(question, predict_answer, ground_truth_answer)
                if score:
                    rewards_per_func[i][0] = 1 - self.args.action_cost * actions[i]
                else:
                    if has_answer[i]:
                        rewards_per_func[i][0] = 0
                    else:
                        rewards_per_func[i][0] = 0 - self.args.action_cost * actions[i]
        #     rewards_per_func = torch.zeros(len(prompts), len(self.reward_funcs), device=device)
        #     for i, (reward_func, reward_processing_class) in enumerate(
        #         zip(self.reward_funcs, self.reward_processing_classes)
        #     ):
        #         if isinstance(reward_func, nn.Module):  # Module instead of PretrainedModel for compat with compiled models
        #             reward_func_name = f"reward {reward_func.config._name_or_path.split('/')[-1]}"
        #         else:
        #             reward_func_name = reward_func.__name__
        #         with profiling_context(self, reward_func_name):
        #             if isinstance(
        #                 reward_func, nn.Module
        #             ):  # Module instead of PretrainedModel for compat with compiled models
        #                 if is_conversational(inputs[0]):
        #                     messages = [{"messages": p + c} for p, c in zip(prompts, completions)]
        #                     texts = [apply_chat_template(x, reward_processing_class)["text"] for x in messages]
        #                 else:
        #                     texts = [p + c for p, c in zip(prompts, completions)]
        #                 reward_inputs = reward_processing_class(
        #                     text=texts, return_tensors="pt", padding=True, padding_side="right", add_special_tokens=False
        #                 )
        #                 reward_inputs = super()._prepare_inputs(reward_inputs)
        #                 with torch.inference_mode():
        #                     rewards_per_func[:, i] = reward_func(**reward_inputs).logits[:, 0]  # Shape (B*G,)
        #             else:
        #                 # Repeat all input columns (but "prompt" and "completion") to match the number of generations
        #                 keys = [key for key in inputs[0] if key not in ["prompt", "completion"]]
        #                 reward_kwargs = {key: [example[key] for example in inputs] for key in keys}
        #                 output_reward_func = reward_func(prompts=prompts, completions=completions, **reward_kwargs)
        #                 # Convert None values to NaN
        #                 output_reward_func = [reward if reward is not None else torch.nan for reward in output_reward_func]

        #                 rewards_per_func[:, i] = torch.tensor(output_reward_func, dtype=torch.float32, device=device)

        #     # If all reward functions return None for a given row, issue a detailed warning
        #     if torch.isnan(rewards_per_func).all(dim=1).any():
        #         nan_row_idx = torch.isnan(rewards_per_func).all(dim=1).nonzero(as_tuple=True)[0][0]
        #         row_reward_kwargs = {key: value[nan_row_idx] for key, value in reward_kwargs.items()}
        #         row_reward_kwargs["prompt"] = prompts[nan_row_idx]
        #         row_reward_kwargs["completion"] = completions[nan_row_idx]
        #         warnings.warn(
        #             f"All reward functions returned None for the following kwargs: {row_reward_kwargs}. "
        #             "Please ensure that at least one reward function returns a valid reward."
        #         )

            # Gather the reward per function: this part is crucial, because the rewards are normalized per group and the
            # completions may be distributed across processes
            rewards_per_func = gather(rewards_per_func)

            # Apply weights to each reward function's output and sum
            rewards = (rewards_per_func * self.reward_weights.to(device).unsqueeze(0)).nansum(dim=1)

            # Compute grouped-wise rewards
            mean_grouped_rewards = rewards.view(-1, self.num_generations).mean(dim=1)
            std_grouped_rewards = rewards.view(-1, self.num_generations).std(dim=1)

            # Normalize the rewards to compute the advantages
            mean_grouped_rewards = mean_grouped_rewards.repeat_interleave(self.num_generations, dim=0)
            std_grouped_rewards = std_grouped_rewards.repeat_interleave(self.num_generations, dim=0)
            advantages = rewards - mean_grouped_rewards
            if self.args.scale_rewards:
                advantages = advantages / (std_grouped_rewards + 1e-4)

            # Slice to keep only the local part of the data
            process_slice = slice(
                self.accelerator.process_index * len(conversations),
                (self.accelerator.process_index + 1) * len(conversations),
            )
            advantages = advantages[process_slice]

            # Log the metrics
            mode = "eval" if self.control.should_evaluate else "train"

            # if mode == "train":
            #     self._total_train_tokens += self.accelerator.gather_for_metrics(attention_mask.sum()).sum().item()
            # self._metrics[mode]["num_tokens"] = [self._total_train_tokens]

            # completion_length = self.accelerator.gather_for_metrics(completion_mask.sum(1)).float().mean().item()
            # self._metrics[mode]["completion_length"].append(completion_length)

            # Calculate mean reward per function, but only for samples where the function was applied
            self._metrics[mode]["reward"].append(rewards.mean().item())
            self._metrics[mode]["reward_std"].append(std_grouped_rewards.mean().item())

            if self.log_completions and self.state.global_step % self.args.logging_steps == 0:
                prompts_to_log = gather_object(prompts_text)
                completions_to_log = gather_object(completions_text)
                rewards_to_log = rewards.tolist()

                if self.accelerator.is_main_process:
                    if is_rich_available():
                        print_prompt_completions_sample(
                            prompts_to_log,
                            completions_to_log,
                            rewards_to_log,
                            self.state.global_step,
                        )
                    if self.args.report_to and "wandb" in self.args.report_to and wandb.run is not None:
                        import pandas as pd

                        # For logging
                        table = {
                            "step": [str(self.state.global_step)] * len(rewards),
                            "prompt": prompts_to_log,
                            "completion": completions_to_log,
                            "reward": rewards.tolist(),
                        }
                        df = pd.DataFrame(table)
                        wandb.log({"completions": wandb.Table(dataframe=df)})


            self.model.audio_tower = self.model.audio_tower.cpu()
            self.model.visual = self.model.visual.cpu()
            return {
                "query_responses": query_responses,
                "attention_masks": attention_masks,
                "position_ids": position_ids,
                "responses": responses,
                "response_masks": response_masks,
                "old_per_token_logps": old_per_token_logps,
                "ref_per_token_logps": ref_per_token_logps,
                "advantages": advantages,
            }

    @profiling_decorator
    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        # model.audio_tower.eval()
        # model.visual.eval()
        if return_outputs:
            raise ValueError("The GRPOTrainer does not support returning outputs")
        # Compute the per-token log probabilities for the model

        query_responses, attention_masks, position_ids = inputs["query_responses"], inputs["attention_masks"], inputs["position_ids"]
        responses, response_masks = inputs["responses"], inputs["response_masks"]
        logits_to_keep = response_masks.size(1)  # we only need to compute the logits for the completion tokens

        per_token_logps = self._get_per_token_logps(model, responses, query_responses, attention_masks, position_ids, logits_to_keep)
        per_token_logps = per_token_logps * response_masks

        # Compute the KL divergence between the model and the reference model
        if self.beta != 0.0:
            ref_per_token_logps = inputs["ref_per_token_logps"]
            ref_per_token_logps = ref_per_token_logps * response_masks

            per_token_kl = (
                torch.exp(ref_per_token_logps - per_token_logps) - (ref_per_token_logps - per_token_logps) - 1
            )

        # Compute the loss
        advantages = inputs["advantages"]
        # When using num_iterations == 1, old_per_token_logps == per_token_logps, so we can skip it's computation (see
        # _generate_and_score_completions) and use per_token_logps.detach() instead.
        # num_iterations($\miu$) = 1 is an on-policy algorithm, and when num_iterations > 1, it is an off-policy algorithm.
        old_per_token_logps = inputs["old_per_token_logps"] if self.num_iterations > 1 else per_token_logps.detach()
        coef_1 = torch.exp(per_token_logps - old_per_token_logps)
        coef_2 = torch.clamp(coef_1, 1 - self.epsilon_low, 1 + self.epsilon_high)
        per_token_loss1 = coef_1 * advantages.unsqueeze(1)
        per_token_loss2 = coef_2 * advantages.unsqueeze(1)
        per_token_loss = -torch.min(per_token_loss1, per_token_loss2)
        if self.beta != 0.0:
            per_token_loss = per_token_loss + self.beta * per_token_kl
        loss = ((per_token_loss * response_masks).sum(-1) / response_masks.sum(-1).clamp(min=1.0)).mean()
        # Log the metrics
        mode = "eval" if self.control.should_evaluate else "train"

        if self.beta != 0.0:
            mean_kl = (per_token_kl * response_masks).sum() / response_masks.sum()
            self._metrics[mode]["kl"].append(self.accelerator.gather_for_metrics(mean_kl).mean().item())

        # is_clipped = (per_token_loss1 < per_token_loss2).float()
        # clip_ratio = (is_clipped * completion_mask).sum() / completion_mask.sum()
        # self._metrics[mode]["clip_ratio"].append(self.accelerator.gather_for_metrics(clip_ratio).mean().item())
        return loss

    def prediction_step(self, model, inputs, prediction_loss_only, ignore_keys: Optional[list[str]] = None):
        inputs = self._prepare_inputs(inputs)
        with torch.no_grad():
            with self.compute_loss_context_manager():
                loss = self.compute_loss(model, inputs)
            loss = loss.mean().detach()
        return loss, None, None

    def log(self, logs: dict[str, float], start_time: Optional[float] = None) -> None:
        mode = "eval" if self.control.should_evaluate else "train"
        metrics = {key: sum(val) / len(val) for key, val in self._metrics[mode].items()}  # average the metrics

        # This method can be called both in training and evaluation. When called in evaluation, the keys in `logs`
        # start with "eval_". We need to add the prefix "eval_" to the keys in `metrics` to match the format.
        if mode == "eval":
            metrics = {f"eval_{key}": val for key, val in metrics.items()}

        logs = {**logs, **metrics}
        if version.parse(transformers.__version__) >= version.parse("4.47.0.dev0"):
            super().log(logs, start_time)
        else:  # transformers<=4.46
            super().log(logs)
        self._metrics[mode].clear()

    def create_model_card(
        self,
        model_name: Optional[str] = None,
        dataset_name: Optional[str] = None,
        tags: Union[str, list[str], None] = None,
    ):
        """
        Creates a draft of a model card using the information available to the `Trainer`.

        Args:
            model_name (`str` or `None`, *optional*, defaults to `None`):
                Name of the model.
            dataset_name (`str` or `None`, *optional*, defaults to `None`):
                Name of the dataset used for training.
            tags (`str`, `list[str]` or `None`, *optional*, defaults to `None`):
                Tags to be associated with the model card.
        """
        if not self.is_world_process_zero():
            return

        if hasattr(self.model.config, "_name_or_path") and not os.path.isdir(self.model.config._name_or_path):
            base_model = self.model.config._name_or_path
        else:
            base_model = None

        tags = tags or []
        if isinstance(tags, str):
            tags = [tags]

        if hasattr(self.model.config, "unsloth_version"):
            tags.append("unsloth")

        citation = textwrap.dedent(
            """\
            @article{zhihong2024deepseekmath,
                title        = {{DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models}},
                author       = {Zhihong Shao and Peiyi Wang and Qihao Zhu and Runxin Xu and Junxiao Song and Mingchuan Zhang and Y. K. Li and Y. Wu and Daya Guo},
                year         = 2024,
                eprint       = {arXiv:2402.03300},
            }
            """
        )

        model_card = generate_model_card(
            base_model=base_model,
            model_name=model_name,
            hub_model_id=self.hub_model_id,
            dataset_name=dataset_name,
            tags=tags,
            wandb_url=wandb.run.get_url() if is_wandb_available() and wandb.run is not None else None,
            comet_url=get_comet_experiment_url(),
            trainer_name="GRPO",
            trainer_citation=citation,
            paper_title="DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models",
            paper_id="2402.03300",
        )

        model_card.save(os.path.join(self.args.output_dir, "README.md"))
