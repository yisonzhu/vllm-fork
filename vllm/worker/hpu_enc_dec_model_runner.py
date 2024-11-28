import math
import gc
import dataclasses
import itertools
from array import array
from functools import partial
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type, cast

import torch

import habana_frameworks.torch as htorch
from vllm_hpu_extension.ops import batch2block, block2batch

from vllm.attention import AttentionMetadata
from vllm.model_executor import SamplingMetadata
from vllm.model_executor.layers.sampler import SamplerOutput
from vllm.multimodal import MultiModalKwargs
from vllm.sequence import SequenceData, IntermediateTensors, SequenceGroupMetadata
from vllm.sampling_params import SamplingParams
from vllm.logger import init_logger
from vllm.utils import is_fake_hpu
from vllm.worker.hpu_model_runner import (subtuple, setup_profiler,
                                          HPUModelRunnerBase, HpuModelAdapter,
                                          ModelInputForHPUWithSamplingMetadata)
from vllm.worker.model_runner_base import (
    _add_attn_metadata_broadcastable_dict,
    _add_sampling_metadata_broadcastable_dict)

if TYPE_CHECKING:
    from vllm.attention.backends.abstract import AttentionBackend

logger = init_logger(__name__)

_TYPE_CACHE = {}
# These values are assumed to be zero in several places.
# Use caution when updating them!
_PAD_SLOT_ID = 0
_PAD_BLOCK_ID = 0


class HpuModelAdapterEncoderDecoder(HpuModelAdapter):

    def __init__(self, model, block_size, dtype, enforce_eager):
        super().__init__(model, block_size, dtype, enforce_eager)

        # We only wrap the language model in HPU graph because some Ops in vision model will fallback to CPU and cause the graph building fail.
        if htorch.utils.internal.is_lazy() and hasattr(self.model,
                                                       "language_model"):
            self.model.language_model = htorch.hpu.wrap_in_hpu_graph(
                self.model.language_model, disable_tensor_cache=True)

    def _set_cross_block_mapping(self, metadata, batch_size, device, dtype):
        mask = torch.arange(0,
                            self.block_size,
                            device=device,
                            dtype=torch.int32).unsqueeze(0)

        cross_attn_mask = mask >= metadata.cross_block_usage.unsqueeze(-1)
        cross_attn_bias = (torch.zeros_like(cross_attn_mask,
                                            dtype=dtype).masked_fill_(
                                                cross_attn_mask, -math.inf))

        if not is_fake_hpu() and htorch.utils.internal.is_lazy():
            cross_block_mapping = torch.nn.functional.one_hot(
                metadata.cross_block_groups, num_classes=batch_size)
        else:
            # Unfortunately one_hot on CPU/torch.compile mode/eager mode
            # doesn't handle out of bounds classes so we need to convert
            # all negative values to 0 (block_mapping) or bs (block_groups)
            cross_block_groups = metadata.cross_block_groups.to(torch.long)
            cross_block_mapping = torch.nn.functional.relu(cross_block_groups)
            cross_block_mapping = torch.nn.functional.one_hot(
                cross_block_mapping, num_classes=batch_size)
            oob_values = cross_block_groups.lt(0)
            cross_block_mapping.masked_fill_(oob_values.unsqueeze(-1), 0)
            cross_block_groups.masked_fill_(oob_values, batch_size)
            metadata = metadata._replace(cross_block_groups=cross_block_groups)

        cross_block_mapping = cross_block_mapping.to(dtype)
        metadata = metadata._replace(cross_block_mapping=cross_block_mapping,
                                     cross_attn_bias=cross_attn_bias)
        return metadata

    def _set_cross_block_scales(self, metadata, device):
        cross_block_mapping = metadata.cross_block_mapping
        ones = torch.ones((cross_block_mapping.size(0), ),
                          device=device,
                          dtype=cross_block_mapping.dtype)
        sums = batch2block(block2batch(ones, cross_block_mapping),
                           cross_block_mapping)
        cross_block_scales = torch.reciprocal(torch.maximum(ones, sums))
        metadata = metadata._replace(cross_block_scales=cross_block_scales)
        return metadata

    def _set_cross_indices_and_offsets(self, metadata, block_size):
        cross_slot_mapping = metadata.cross_slot_mapping.flatten()
        indices = torch.div(cross_slot_mapping,
                            block_size,
                            rounding_mode="floor")
        offsets = torch.fmod(cross_slot_mapping, block_size)
        metadata = metadata._replace(cross_block_offsets=offsets,
                                     cross_block_indices=indices)
        return metadata

    def _update_cross_metadata(self, attn_metadata, batch_size, device, dtype):
        if max(attn_metadata.encoder_seq_lens) == 0:
            return attn_metadata
        if attn_metadata.is_prompt:
            attn_metadata = self._set_cross_indices_and_offsets(
                attn_metadata, self.block_size)
        else:
            attn_metadata = self._set_cross_block_mapping(
                attn_metadata, batch_size, device, dtype)
            attn_metadata = self._set_cross_block_scales(attn_metadata, device)

        return attn_metadata

    def forward(self, *args, **kwargs):
        kwargs = kwargs.copy()
        selected_token_indices = kwargs.pop('selected_token_indices')
        if 'warmup_mode' in kwargs:
            kwargs.pop('warmup_mode')
        input_ids = kwargs['input_ids']
        kwargs['attn_metadata'] = self._update_metadata(
            kwargs['attn_metadata'], input_ids.size(0), input_ids.size(1),
            input_ids.device, self.dtype)
        kwargs['attn_metadata'] = self._update_cross_metadata(
            kwargs['attn_metadata'], input_ids.size(0), input_ids.device,
            self.dtype)
        if htorch.utils.internal.is_lazy() and hasattr(self.model,
                                                       "language_model"):
            bypass_hpu_graphs = kwargs.get('bypass_hpu_graphs', False)
            self.model.language_model.forward = partial(self.model.language_model.forward, bypass_hpu_graphs=bypass_hpu_graphs)
        # Change the input_ids to 1D to match the public vllm implementation
        # and avoid shape mismatch issues with some models.
        # kwargs['input_ids'] = input_ids.flatten()
        hidden_states = self.model(*args, **kwargs)
        hidden_states = hidden_states.view(-1, hidden_states.shape[-1])
        hidden_states = hidden_states.index_select(0, selected_token_indices)
        return hidden_states


@dataclasses.dataclass(frozen=True)
class EncoderDecoderModelInputForHPU(ModelInputForHPUWithSamplingMetadata):
    """
    Used by the EncoderDecoderModelRunner.
    """
    encoder_input_tokens: Optional[torch.Tensor] = None
    encoder_input_positions: Optional[torch.Tensor] = None

    def as_broadcastable_tensor_dict(self) -> Dict[str, Any]:
        tensor_dict = {
            "input_tokens": self.input_tokens,
            "input_positions": self.input_positions,
            "encoder_input_tokens": self.encoder_input_tokens,
            "encoder_input_positions": self.encoder_input_positions,
        }
        _add_attn_metadata_broadcastable_dict(tensor_dict, self.attn_metadata)
        _add_sampling_metadata_broadcastable_dict(tensor_dict,
                                                  self.sampling_metadata)
        return tensor_dict

    @classmethod
    def from_broadcasted_tensor_dict(
        cls,
        tensor_dict: Dict[str, Any],
        attn_backend: Optional["AttentionBackend"] = None,
    ) -> "EncoderDecoderModelInputForHPU":
        return cast(
            EncoderDecoderModelInputForHPU,
            super().from_broadcasted_tensor_dict(tensor_dict, attn_backend))


class HPUEncoderDecoderModelRunner(
        HPUModelRunnerBase[EncoderDecoderModelInputForHPU]):
    _model_input_cls: Type[EncoderDecoderModelInputForHPU] = (
        EncoderDecoderModelInputForHPU)
    _model_adapter_cls: Type[HpuModelAdapterEncoderDecoder] = (
        HpuModelAdapterEncoderDecoder)

    def _list_to_int32_tensor(
        self,
        _list: List[int],
    ) -> torch.Tensor:
        return torch.tensor(_list, dtype=torch.int32, device=self.device)

    def _list_to_long_tensor(
        self,
        _list: List[int],
    ) -> torch.Tensor:
        return torch.tensor(_list, dtype=torch.long, device=self.device)

    def make_model_input_from_broadcasted_tensor_dict(
            self, tensor_dict: Dict[str,
                                    Any]) -> EncoderDecoderModelInputForHPU:
        return EncoderDecoderModelInputForHPU.from_broadcasted_tensor_dict(
            tensor_dict,
            attn_backend=self.attn_backend,
        )

    def _flatten(self, in_list):
        return list(itertools.chain(*in_list))

    def _maybe_wrap_in_hpu_graph(self, *args, **kwargs):
        return HpuModelAdapterEncoderDecoder(*args, **kwargs)

    def prepare_model_input(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        virtual_engine: int = 0,
        finished_requests_ids: Optional[List[str]] = None
    ) -> EncoderDecoderModelInputForHPU:
        with self.profiler.record_event('internal', 'prepare_input_tensors'):
            assert seq_group_metadata_list is not None
            if self.profiler.enabled:
                self.profiler_counter_helper.capture_seq_group_metadata_stats(
                    seq_group_metadata_list=seq_group_metadata_list)
            model_input, sampling_metadata = self.prepare_input_tensors(
                seq_group_metadata_list)
            attn_metadata = self._prepare_encoder_model_input_tensors(
                seq_group_metadata_list, model_input)
            model_input = dataclasses.replace(
                model_input,
                attn_metadata=attn_metadata,
            )
            assert model_input.attn_metadata is not None
            is_prompt = model_input.attn_metadata.is_prompt

        return dataclasses.replace(model_input,
                                   sampling_metadata=sampling_metadata,
                                   is_prompt=is_prompt,
                                   virtual_engine=virtual_engine)

    def _prepare_encoder_model_input_tensors(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        model_input: ModelInputForHPUWithSamplingMetadata,
    ):
        if len(seq_group_metadata_list) == 0:
            return None

        # Since we are not supporting chunked prefill either the entire
        # batch is prefill or it is decode
        is_prompt = seq_group_metadata_list[0].is_prompt
        # Build encoder inputs
        encoder_seq_lens: List[int] = []
        cross_block_tables: List[List[int]] = []
        cross_slot_mapping: List[int] = []
        attn_metadata = model_input.attn_metadata
        assert attn_metadata is not None
        if is_prompt:
            for seq_group_metadata in seq_group_metadata_list:
                # Build seq lens
                seq_len = seq_group_metadata.encoder_seq_data.get_len(
                ) if seq_group_metadata.encoder_seq_data else 0
                encoder_seq_lens.append(seq_len)
                # Build slot mapping
                if seq_group_metadata.cross_block_table is None:
                    cross_slot_mapping.extend([_PAD_SLOT_ID] * seq_len)
                else:
                    for i in range(0, seq_len):
                        block_number = seq_group_metadata.cross_block_table[
                            i // self.block_size]
                        block_offset = i % self.block_size
                        slot = block_number * self.block_size + block_offset
                        cross_slot_mapping.append(slot)
            attn_metadata.cross_slot_mapping = torch.tensor(cross_slot_mapping,
                                                            dtype=torch.long,
                                                            device=self.device)
        else:
            for seq_group_metadata in seq_group_metadata_list:
                for _ in range(len(seq_group_metadata.seq_data)):
                    seq_len = seq_group_metadata.encoder_seq_data.get_len(
                    ) if seq_group_metadata.encoder_seq_data else 0
                    encoder_seq_lens.append(seq_len)
                    cross_block_table = seq_group_metadata.cross_block_table
                    cross_block_tables.append([] if (
                        cross_block_table is None) else cross_block_table)

            last_block_usage = [(seq_len - 1) % self.block_size + 1
                                for seq_len in encoder_seq_lens]
            block_groups = [[i] * len(bt)
                            for i, bt in enumerate(cross_block_tables)]
            block_usage = [
                [self.block_size] * (len(bt) - 1) + [lbu]
                for bt, lbu in zip(cross_block_tables, last_block_usage) if bt
            ]

            block_list = self._flatten(cross_block_tables)
            block_groups = self._flatten(block_groups)
            block_usage = self._flatten(block_usage)

            assert len(block_list) == len(block_groups)
            assert len(block_list) == len(block_usage)

            block_list = torch.tensor(block_list,
                                      dtype=torch.int,
                                      device='cpu')
            block_groups = torch.tensor(block_groups,
                                        dtype=torch.int,
                                        device='cpu')
            block_usage = torch.tensor(block_usage,
                                       dtype=self.model_config.dtype,
                                       device='cpu')

            block_list = block_list.to(  # type: ignore
                self.device, non_blocking=True)
            block_groups = block_groups.to(  # type: ignore
                self.device, non_blocking=True)
            block_usage = block_usage.to(  # type: ignore
                self.device, non_blocking=True)

            attn_metadata.cross_block_list = block_list
            attn_metadata.cross_block_groups = block_groups
            attn_metadata.cross_block_usage = block_usage

        encoder_seq_lens_tensor = self._list_to_int32_tensor(
            encoder_seq_lens)
        attn_metadata.encoder_seq_lens = encoder_seq_lens
        attn_metadata.encoder_seq_lens_tensor = encoder_seq_lens_tensor

        return attn_metadata

    def profile_run(self) -> None:
        num_layers = self.model_config.get_num_layers(self.parallel_config)
        kv_caches = [None] * num_layers
        max_batch_size = min(self.bucketing_global_state.prompt_bs_bucket_cfg[-1],
                             self.scheduler_config.max_num_seqs)
        max_seq_len = self.max_num_batched_tokens // max_batch_size

        self.warmup_scenario(max_batch_size, max_seq_len, True, kv_caches,
                             False)
        return

    def warmup_scenario(self,
                        batch_size,
                        seq_len,
                        is_prompt,
                        kv_caches,
                        is_pt_profiler_run=False) -> None:
        use_graphs = self._use_graphs(batch_size, seq_len, is_prompt)
        scenario_name = ("warmup_"
                         f"{'prompt' if is_prompt else 'decode'}_"
                         f"bs{batch_size}_"
                         f"seq{seq_len}_"
                         f"graphs{'T' if use_graphs else 'F'}")
        self.profiler.start('internal', scenario_name)
        times = 3 if use_graphs or is_pt_profiler_run else 1
        if is_prompt:
            seqs = [
                self.create_dummy_seq_group_metadata(
                    i,
                    seq_len,
                    is_prompt)
                for i in range(batch_size)
            ]
        else:
            # FIXME: seq_len is actually number of blocks
            blocks = [seq_len // batch_size for _ in range(batch_size)]
            blocks[0] += seq_len % batch_size
            seqs = [
                self.create_dummy_seq_group_metadata(
                    i,
                    b * self.block_size - 1,
                    is_prompt)
                for i, b in enumerate(blocks)
            ]
        torch.hpu.synchronize()
        profiler = None
        if is_pt_profiler_run and self.is_driver_worker:
            profiler = setup_profiler()
            profiler.start()
        for _ in range(times):
            inputs = self.prepare_model_input(seqs)
            self.execute_model(inputs, kv_caches, warmup_mode=True)
            torch.hpu.synchronize()
            if profiler:
                profiler.step()
        if profiler:
            profiler.stop()
        self.profiler.end()
        gc.collect()

    def create_dummy_seq_group_metadata(self,
                                        group_id,
                                        seq_len,
                                        is_prompt):
        sampling_params = SamplingParams(temperature=0)
        num_blocks = math.ceil(seq_len / self.block_size)
        cross_block_table: List[int] = None
        encoder_dummy_data \
            = self.input_registry.dummy_data_for_profiling(
                self.model_config,
                                        seq_len,
                                        self.mm_registry,
                                        is_encoder_data=True)
        mm_counts = self.mm_registry.get_mm_limits_per_prompt(self.model_config)
        num_images = mm_counts["image"]
        max_mm_tokens = self.mm_registry.get_max_multimodal_tokens(self.model_config) * num_images
        num_cross_blocks = math.ceil(max_mm_tokens / self.block_size)
        seq_len = max(seq_len, 1)
        if is_prompt:
            input_len = seq_len
            output_len = 0
            block_tables = None
            cross_block_table = None
        else:
            input_len = seq_len - 1
            output_len = 1
            block_tables = {group_id: [_PAD_BLOCK_ID] * num_blocks}
            cross_block_table = [_PAD_BLOCK_ID] * num_cross_blocks
        prompt_token_ids = [0] * input_len
        output_token_ids = [1] * output_len
        prompt_token_ids_array = array('l', prompt_token_ids)  # noqa: F821
        seq_data = SequenceData(prompt_token_ids_array)
        seq_data.output_token_ids = output_token_ids
        return SequenceGroupMetadata(request_id=str(group_id),
                                     is_prompt=(output_len == 0),
                                     seq_data={group_id: seq_data},
                                     sampling_params=sampling_params,
                                     block_tables=block_tables,
                                     encoder_seq_data=encoder_dummy_data.seq_data,
                                     multi_modal_data=encoder_dummy_data.multi_modal_data,
                                     cross_block_table=cross_block_table)

    def trim_attn_metadata(self, metadata: AttentionMetadata) -> object:
        # NOTE(kzawora): To anyone working on this in the future:
        # Trimming metadata is required when using HPUGraphs.
        # Attention metadata is going to be hashed by PT bridge, and
        # appropriate HPUGraphs will be matched based on all inputs' hash.

        # Before you put more keys in here, make sure you know their
        # value type and make sure you know how it's going to be hashed.
        # You can find that information in input_hash function
        # in habana_frameworks/torch/hpu/graphs.py. You can also hash
        # it manually with torch.hpu.graphs.input_hash(attention_metadata)

        # If you use primitive types here - they will get hashed based
        # on their value. You *will* get lots of excessive graph captures
        # (and an OOM eventually) if you decide to put something like
        # seq_len int here.
        # If you absolutely need a scalar, put it in a tensor. Tensors
        # get hashed using their metadata, not their values:
        # input_hash(torch.tensor(123)) == input_hash(torch.tensor(321))
        # input_hash(123) != input_hash(321)
        # input_hash("abc") != input_hash("cba")
        attention_metadata = subtuple(metadata, 'TrimmedAttentionMetadata', [
            'attn_bias',
            'seq_lens_tensor',
            'context_lens_tensor',
            'block_list',
            'block_mapping',
            'block_usage',
            'slot_mapping',
            'is_prompt',
            'block_indices',
            'block_offsets',
            'block_scales',
            'block_groups',
            'num_prefill_tokens',
            'num_decode_tokens',
            'num_prefills',
            'seq_lens',
            'encoder_seq_lens',
            'encoder_seq_lens_tensor',
            'cross_block_indices',
            'cross_block_offsets',
            'cross_block_list',
            'cross_slot_mapping',
            'cross_block_mapping',
            'cross_block_groups',
            'cross_block_scales',
            'cross_block_usage',
            'cross_attn_bias',
        ])
        return attention_metadata

    def _check_config(self, batch_size, seq_len, is_prompt, warmup_mode):
        cfg = (batch_size, seq_len, is_prompt)
        seen = cfg in self.seen_configs
        self.seen_configs.add(cfg)
        if not seen and not warmup_mode:
            phase = 'prompt' if is_prompt else 'decode'
            logger.warning("Configuration: (%s, %s, %s) was not warmed-up!",
                           phase, batch_size, seq_len)

    @torch.inference_mode()
    def execute_model(
        self,
        model_input: ModelInputForHPUWithSamplingMetadata,
        kv_caches: List[torch.Tensor],
        intermediate_tensors: Optional[IntermediateTensors] = None,
        num_steps: int = 1,
        warmup_mode=False,
    ) -> Optional[List[SamplerOutput]]:
        if num_steps > 1:
            raise ValueError(
                "num_steps > 1 is not supported in HPUEncoderDecoderModelRunner")

        input_tokens = model_input.input_tokens
        input_positions = model_input.input_positions
        attn_metadata = model_input.attn_metadata
        sampling_metadata = model_input.sampling_metadata
        real_batch_size = model_input.real_batch_size
        batch_size_padded = model_input.batch_size_padded
        assert input_tokens is not None
        assert input_positions is not None
        assert sampling_metadata is not None
        assert attn_metadata is not None
        is_prompt = attn_metadata.is_prompt
        assert is_prompt is not None
        batch_size = input_tokens.size(0)
        seq_len = self._seq_len(attn_metadata)
        use_graphs = self._use_graphs(batch_size, seq_len, is_prompt)
        self._check_config(batch_size, seq_len, is_prompt, warmup_mode)

        execute_model_kwargs = {
            "input_ids": input_tokens,
            "positions": input_positions,
            "kv_caches": kv_caches,
            "attn_metadata": self.trim_attn_metadata(attn_metadata),
            "intermediate_tensors": intermediate_tensors,
            **(model_input.multi_modal_kwargs or {}),
        }
        if htorch.utils.internal.is_lazy():
            execute_model_kwargs.update({"bypass_hpu_graphs": not use_graphs})

        htorch.core.mark_step()
        if self.is_driver_worker:
            model_event_name = ("model_"
                                f"{'prompt' if is_prompt else 'decode'}_"
                                f"bs{batch_size}_"
                                f"seq{seq_len}_"
                                f"graphs{'T' if use_graphs else 'F'}")
        else:
            model_event_name = 'model_executable'
        with self.profiler.record_event('internal', model_event_name):
            hidden_states = self.model.forward(
                **execute_model_kwargs,
                selected_token_indices=sampling_metadata.selected_token_indices
            )

        # Compute the logits.
        with self.profiler.record_event(
                'internal', ('compute_logits_'
                             f'{"prompt" if is_prompt else "decode"}_bs'
                             f'{batch_size}_'
                             f'seq{seq_len}')):
            sampling_metadata.selected_token_indices = None
            logits = self.model.compute_logits(hidden_states,
                                               sampling_metadata)
        htorch.core.mark_step()
        # Only perform sampling in the driver worker.
        if not self.is_driver_worker:
            return []

        if model_input.async_callback is not None:
            model_input.async_callback()

        # Sample the next token.
        with self.profiler.record_event(
                'internal', ('sample_'
                             f'{"prompt" if is_prompt else "decode"}_'
                             f'bs{batch_size}_'
                             f'seq{seq_len}')):
            output = self.model.sample(
                logits=logits,
                sampling_metadata=sampling_metadata,
            )
        output.outputs = output.outputs[:real_batch_size]
        htorch.core.mark_step()

        if self.is_driver_worker and self.profiler.enabled:
            # Stop recording 'execute_model' event
            self.profiler.end()
            event_end = self.profiler.get_timestamp_us()
            counters = self.profiler_counter_helper.get_counter_dict(
                cache_config=self.cache_config,
                duration=event_end - self.event_start,
                seq_len=seq_len,
                batch_size_padded=batch_size_padded,
                real_batch_size=real_batch_size,
                is_prompt=is_prompt)
            self.profiler.record_counter(self.event_start, counters)
        return [output]
