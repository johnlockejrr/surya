from typing import Optional, Tuple, TypedDict
from dataclasses import dataclass

import torch
from torch import nn
import torch.nn.functional as F
from transformers import PreTrainedModel
from transformers.modeling_outputs import CausalLMOutputWithPast
from transformers.cache_utils import Cache
from transformers.modeling_attn_mask_utils import AttentionMaskConverter

from surya.common.s3 import S3DownloaderMixin
from surya.common.surya.config import SuryaModelConfig
from surya.common.surya.decoder import SuryaDecoderModel
from surya.common.surya.embedder import SimpleTokenEmbedder
from surya.common.surya.encoder import SuryaEncoderModel
from surya.common.xla import get_nearest_pad
from surya.settings import settings

from transformers.utils import is_flash_attn_2_available

from surya.logging import get_logger

if is_flash_attn_2_available():
    from surya.common.surya.flash_attn_utils import _get_unpad_data

logger = get_logger()


@dataclass
class SuryaModelOutput(CausalLMOutputWithPast):
    bbox_logits: torch.FloatTensor = None
    lm_logits: torch.FloatTensor = None


class FlashAttentionKwargs(TypedDict, total=False):
    """
    Keyword arguments for Flash Attention with Compile.

    Attributes:
        cu_seq_lens_q (`torch.LongTensor`, *optional*)
            Gets cumlative sequence length for query state.
        cu_seq_lens_k (`torch.LongTensor`, *optional*)
            Gets cumlative sequence length for key state.
        max_length_q (`int`, *optional*):
            Maximum sequence length for query state.
        max_length_k (`int`, *optional*):
            Maximum sequence length for key state.
    """

    cu_seq_lens_q: Optional[torch.LongTensor]
    cu_seq_lens_k: Optional[torch.LongTensor]
    max_length_q: Optional[int]
    max_length_k: Optional[int]


class KwargsForCausalLM(FlashAttentionKwargs): ...


class DistanceProjection(nn.Module):
    def __init__(self, in_features: int, out_features: int):
        super().__init__()
        self.fc1 = nn.Linear(in_features, out_features)
        self.act = nn.SiLU()
        self.fc2 = nn.Linear(out_features, out_features)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.fc1(x)
        x = self.act(x)
        x = self.fc2(x)
        return x

    def init_weights(self):
        nn.init.xavier_uniform_(self.fc1.weight)
        nn.init.xavier_uniform_(self.fc2.weight)
        nn.init.zeros_(self.fc1.bias)
        nn.init.zeros_(self.fc2.bias)


class SuryaModel(S3DownloaderMixin, PreTrainedModel):
    config_class = SuryaModelConfig
    supports_gradient_checkpointing = True
    _skip_keys_device_placement = ["past_key_values"]
    _supports_flash_attn_2 = True
    _supports_sdpa = True
    _supports_flex_attn = True
    _supports_cache_class = True
    _supports_quantized_cache = True
    _supports_static_cache = True
    _supports_attention_backend = True
    main_input_name = "input_ids"
    _tied_weights_keys = ["lm_head.weight"]

    def __init__(
        self,
        config: SuryaModelConfig,
        embedder: SimpleTokenEmbedder = None,
        vision_encoder: SuryaEncoderModel = None,
        decoder: SuryaDecoderModel = None,
    ):
        super().__init__(config)

        if vision_encoder is None:
            vision_encoder = SuryaEncoderModel(config.vision_encoder)

        if decoder is None:
            decoder = SuryaDecoderModel(config.decoder)

        if embedder is None:
            embedder = SimpleTokenEmbedder(config)

        self.vision_encoder = vision_encoder
        self.decoder = decoder
        self.embedder = embedder

        # Simple encoding for image patches
        self.img_w_embed = nn.Embedding(
            self.config.image_embed_encoding_size,
            self.config.hidden_size,
        )

        self.img_h_embed = nn.Embedding(
            self.config.image_embed_encoding_size,
            self.config.hidden_size,
        )

        # Tying configs
        self.vision_encoder.config = self.config.vision_encoder
        self.decoder.config = self.config.decoder

        self.bbox_head = nn.Linear(config.hidden_size, 6)
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size)

        if (
            self.config.multi_output_distance is not None
            and self.config.multi_output_distance > 0
        ):
            self.multi_output_projections = nn.ModuleList(
                [
                    DistanceProjection(
                        in_features=config.hidden_size, out_features=config.hidden_size
                    )
                    for _ in range(self.config.multi_output_distance)
                ]
            )

    def tie_weights(self):
        self._tie_weights()

    def _tie_weights(self):
        # Tie weights of lm head and token embedder
        self._tie_or_clone_weights(self.lm_head, self.embedder.token_embed)

    def get_output_embeddings(self) -> nn.Module:
        return self.lm_head

    def get_input_embeddings(self) -> nn.Module:
        return self.embedder.token_embed

    def set_output_embeddings(self, new_embeddings: nn.Module):
        self.lm_head = new_embeddings

    def set_input_embeddings(self, new_embeddings: nn.Module):
        self.embedder.token_embed = new_embeddings

    def maybe_static_pad_image_inputs(
        self,
        chunk_pixels: torch.Tensor,
        chunk_grid_thw: torch.Tensor,
        actual_chunk_len: int,
        encoder_chunk_size: int,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        valid_embed_len = actual_chunk_len // (
            self.vision_encoder.spatial_merge_size**2
        )
        if settings.FOUNDATION_STATIC_CACHE and actual_chunk_len < encoder_chunk_size:
            padding_len = encoder_chunk_size - actual_chunk_len
            chunk_pixels = F.pad(
                chunk_pixels,
                (0, 0, 0, padding_len),
                mode="constant",
                value=0.0,
            )

            padding_grid = torch.tensor(
                [[1, 2, padding_len // 2]],
                device=chunk_grid_thw.device,
                dtype=chunk_grid_thw.dtype,
            )
            chunk_grid_thw = torch.cat([chunk_grid_thw, padding_grid], dim=0)

        return chunk_pixels, chunk_grid_thw, valid_embed_len

    """
    def get_image_embeddings(
        self,
        pixel_values: torch.Tensor,
        grid_thw: torch.Tensor,
        encoder_chunk_size: int | None,
        image_tile_length: torch.Tensor | None = None,
        valid_batch_size: torch.Tensor | None = None,
    ):
        # embed all images with the vision encoder after they have already been tiled and flattened into a single batch
        chunks = [0]
        grid_chunks = [0]
        curr_chunk_len = 0
        curr_seq_len = 0
        chunk_tokens = []
        grid_chunk_size = []
        curr_grid_len = 0
        for i in range(len(grid_thw[:valid_batch_size])):
            curr_sample_len = grid_thw[i][0] * grid_thw[i][1] * grid_thw[i][2]

            if (
                curr_chunk_len > (encoder_chunk_size - curr_sample_len)
                and curr_chunk_len > 0
            ):
                chunk_tokens.append(curr_chunk_len)
                chunks.append(curr_chunk_len + curr_seq_len)
                curr_seq_len += curr_chunk_len
                curr_chunk_len = 0
                grid_chunks.append(i)
                grid_chunk_size.append(curr_grid_len)
                curr_grid_len = 0

            curr_chunk_len += curr_sample_len
            curr_grid_len += 1

        if curr_chunk_len > 0:
            chunks.append(image_tile_length)
            grid_chunks.append(valid_batch_size)
            chunk_tokens.append(curr_chunk_len)
            grid_chunk_size.append(curr_grid_len)

        assert curr_chunk_len + curr_seq_len == image_tile_length, (
            f"Mismatch in encoder chunking, {curr_chunk_len} + {curr_seq_len} != {image_tile_length}"
        )

        logger.debug(
            f"Chunking encoder sequence into {len(chunks) - 1} chunks of size {encoder_chunk_size} with lengths {chunks} and grids {grid_chunks}"
        )

        grid_thw = grid_thw.to(self.device)
        chunk_embedding_lst = []
        for i in range(len(chunks) - 1):
            start = chunks[i]
            end = chunks[i + 1]
            grid_start = grid_chunks[i]
            grid_end = grid_chunks[i + 1]

            chunk_pixels = pixel_values[start:end]
            chunk_grid_thw = grid_thw[grid_start:grid_end]
            actual_chunk_len = end - start
            logger.debug(f"Starting to inference chunk {i}")
            chunk_pixels, chunk_grid_thw, valid_embed_len = (
                self.maybe_static_pad_image_inputs(
                    chunk_pixels, chunk_grid_thw, actual_chunk_len, encoder_chunk_size
                )
            )
            logger.debug(
                f"Inferencing chunk {i} with size {chunk_pixels.shape} and grid {chunk_grid_thw.shape}"
            )

            chunk_embeddings = self.vision_encoder.embed_images(
                image_batch=chunk_pixels.to(self.device), grid_thw=chunk_grid_thw
            )
            chunk_embedding_lst.append(chunk_embeddings[:valid_embed_len])

        embeddings = torch.cat(chunk_embedding_lst, dim=0)

        encoding_2d = self.get_2d_learned_embeddings(
            grid_thw,
            valid_batch_size=valid_batch_size,
            device=embeddings.device,
            bbox_size=self.config.image_embed_encoding_multiplier,
        )
        assert embeddings.shape[0] == encoding_2d.shape[0], (
            f"Mismatch in image embedding seq len: {embeddings.shape} vs {encoding_2d.shape}"
        )
        assert embeddings.shape[1] == encoding_2d.shape[1], (
            f"Mismatch in image embedding token counts: {embeddings.shape} vs {encoding_2d.shape}"
        )

        embeddings = embeddings + encoding_2d
        return embeddings
    """

    def get_image_embeddings(
        self,
        pixel_values: torch.Tensor,
        grid_thw: torch.Tensor,
        encoder_chunk_size: int | None,
        image_tile_length: torch.Tensor | None = None,
        valid_batch_size: torch.Tensor | None = None,
    ):
        # embed all images with the vision encoder after they have already been tiled and flattened into a single batch
        unpadded_max_grid_size = (
            grid_thw[:, 0] * grid_thw[:, 1] * grid_thw[:, 2]
        ).max()
        max_grid_size = get_nearest_pad(
            unpadded_max_grid_size,
        )  # If we need zero padding, we still need to allocate a bit of room for the extra grid_thw

        if max_grid_size == unpadded_max_grid_size:
            max_grid_size += 16

        full_image_grid = torch.zeros(
            (valid_batch_size, max_grid_size, pixel_values.shape[-1]),
            dtype=pixel_values.dtype,
        )

        # Roll out into a full grid
        seq_len = 0
        row_grids = []
        for i in range(valid_batch_size):
            curr_sample_len = grid_thw[i][0] * grid_thw[i][1] * grid_thw[i][2]
            full_image_grid[i, -curr_sample_len:] = pixel_values[
                seq_len : seq_len + curr_sample_len
            ]
            padded_len = max_grid_size - curr_sample_len
            if padded_len > 0:
                row_grid = torch.tensor(
                    [
                        [1, 4, padded_len // 4],
                        grid_thw[i].tolist(),
                    ],
                    dtype=torch.long,
                )
            else:
                row_grid = torch.tensor(
                    [
                        grid_thw[i].tolist(),
                    ],
                    dtype=torch.long,
                )

            row_grids.append(row_grid)
            seq_len += curr_sample_len

        # bsz, 2, 3
        row_grids = torch.stack(row_grids, dim=0).to(self.device)
        full_image_grid = full_image_grid.to(self.device)
        embeddings = self.vision_encoder.embed_images(
            image_batch=full_image_grid, grid_thw=row_grids
        )

        encoding_2d = self.get_2d_learned_embeddings(
            row_grids,
            bbox_size=self.config.image_embed_encoding_multiplier,
        )
        embeddings += encoding_2d

        return embeddings

    def embed_ids_boxes_images(
        self,
        input_ids,
        image_embeddings,
        encoder_chunk_size: int,
        valid_batch_size: torch.Tensor | None = None,
    ):
        """
        Insert embedded image tiles into the corresponding positions into the full input sequence

        Positions to insert new tokens are indicated by the special image token index
        """
        # This is batched in the inner call
        inputs_embeds = self.embedder.embed(input_tokens=input_ids)

        if image_embeddings is not None:
            image_token_id_tensor = torch.tensor(
                self.config.image_token_id,
                device=inputs_embeds.device,
                dtype=torch.long,
            )
            logger.debug(
                f"Inserting image embeddings at token id {self.config.image_token_id}"
            )
            mask = input_ids == image_token_id_tensor
            last_image_token_pos = (
                mask.size(1)
                - 1
                - mask.flip(dims=[1]).long().argmax(dim=1, keepdim=True)
            )
            # Calculate start position to replace N positions ending at (and including) the last image token
            start_positions = last_image_token_pos - image_embeddings[0].shape[0]

            batch_size, insert_len = image_embeddings.shape[:2]
            batch_indices = torch.arange(
                batch_size, device=inputs_embeds.device
            ).unsqueeze(1)

            # Create position indices for each insertion
            pos_indices = torch.arange(
                insert_len, device=inputs_embeds.device
            ).unsqueeze(0)
            insert_positions = start_positions.unsqueeze(1) + pos_indices

            inputs_embeds[batch_indices, insert_positions] = image_embeddings
        else:
            assert (input_ids == self.config.image_token_id).sum() == 0, (
                "Image tokens were present in the input but no input images were provided"
            )

        inputs_embeds[input_ids == self.config.pad_token_id] = 0
        return inputs_embeds

    def get_2d_learned_embeddings(
        self,
        grid_thw,
        bbox_size: int = 256,
    ):
        dev = grid_thw.device
        all_row_coords = []
        all_col_coords = []
        for row_grid in grid_thw:
            merge = self.config.merge_size

            # per-sample grid sizes after merge
            H = (row_grid[:, 1] // merge).long()  # (B,)
            W = (row_grid[:, 2] // merge).long()  # (B,)

            row_coords = torch.cat(
                [
                    torch.linspace(0, bbox_size, steps=int(h), device=dev)
                    .round()
                    .repeat_interleave(w)  # repeat each row value w times
                    for h, w in zip(H.tolist(), W.tolist())
                ]
            )  # (full_grid_size,)

            col_coords = torch.cat(
                [
                    torch.linspace(0, bbox_size, steps=int(w), device=dev)
                    .round()
                    .repeat(int(h))  # tile the column vector h times
                    for h, w in zip(H.tolist(), W.tolist())
                ]
            )  # (full_grid_size,)
            all_row_coords.append(row_coords)
            all_col_coords.append(col_coords)
        row_coords = torch.stack(all_row_coords, dim=0)
        col_coords = torch.stack(all_col_coords, dim=0)

        emb = self.img_h_embed(row_coords.long()) + self.img_w_embed(col_coords.long())
        return emb

    def get_logits(self, hidden_states):
        assert hidden_states.shape[1] == 1, (
            "Multi output predictions only applied on the last token"
        )

        all_lm_logits = []
        all_bbox_logits = []

        current_hidden = hidden_states

        # Loop includes initial prediction (i=0) plus multi_out_distance additional predictions
        for i in range(self.config.multi_output_distance + 1):
            if i > 0:
                current_hidden = self.multi_output_projections[i - 1](current_hidden)

            lm_logits = self.lm_head(current_hidden)
            bbox_logits = F.sigmoid(self.bbox_head(current_hidden))

            all_lm_logits.append(lm_logits)
            all_bbox_logits.append(bbox_logits)

        # Concatenate along sequence dimension (dim=1)
        final_lm_logits = torch.cat(all_lm_logits, dim=1)
        final_bbox_logits = torch.cat(all_bbox_logits, dim=1)

        return final_lm_logits, final_bbox_logits

    def forward(
        self,
        input_ids=None,
        image_embeddings=None,
        inputs_embeds=None,
        attention_mask=None,
        position_ids=None,
        cache_position=None,
        past_key_values=None,
        output_hidden_states=False,
        output_attentions=False,
        use_cache=False,
        encoder_chunk_size=None,
        cache_idxs=None,
        num_valid_tokens=None,
        prefill=False,
        text_lengths=None,
        valid_batch_size: torch.Tensor = None,
        **kwargs: KwargsForCausalLM,
    ):
        # Process the mixed batch if provided
        if inputs_embeds is None:
            inputs_embeds = self.embed_ids_boxes_images(
                input_ids,
                image_embeddings,
                encoder_chunk_size,
                valid_batch_size,
            )

        # Handling flash attention kwargs outside the decoder to speed up + avoid graph breaks inside the decoder
        # Skipped during decoding since not required
        if self.decoder.config._attn_implementation == "flash_attention_2" and prefill:
            batch_size, query_length, _ = inputs_embeds.shape
            indices_k, cu_seqlens_k, max_seqlen_in_batch_k = _get_unpad_data(
                attention_mask
            )
            kwargs["batch_size"] = batch_size
            kwargs["query_length"] = query_length
            kwargs["indices_k"] = indices_k
            kwargs["cu_seqlens_k"] = cu_seqlens_k
            kwargs["max_seqlen_in_batch_k"] = max_seqlen_in_batch_k

        if cache_position is None:
            past_seen_tokens = (
                past_key_values.get_seq_length() if past_key_values is not None else 0
            )
            cache_position = torch.arange(
                past_seen_tokens,
                past_seen_tokens + inputs_embeds.shape[1],
                device=inputs_embeds.device,
            )

        if position_ids is None:
            position_ids = cache_position.unsqueeze(0)

        causal_mask = self._update_causal_mask(
            attention_mask,
            inputs_embeds,
            cache_position,
            past_key_values,
            output_attentions,
        )

        attention_mask = causal_mask
        outputs = self.decoder(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            position_ids=position_ids,
            cache_position=cache_position,
            past_key_values=past_key_values,
            return_dict=True,
            use_cache=use_cache,
            cache_idxs=cache_idxs,
            num_valid_tokens=num_valid_tokens,
            prefill=prefill,
            text_lengths=text_lengths,
            **kwargs,
        )

        hidden_states = outputs.last_hidden_state
        # Only keep the last `logits_to_keep` logits, should bring down memory usage during inference
        hidden_states = hidden_states[:, -1:, :]
        hidden_states = hidden_states.contiguous()
        lm_logits, bbox_logits = self.get_logits(hidden_states)

        return SuryaModelOutput(
            bbox_logits=bbox_logits,
            lm_logits=lm_logits,
            hidden_states=outputs.hidden_states if output_hidden_states else None,
            attentions=outputs.attentions if output_attentions else None,
            past_key_values=outputs.past_key_values,
        )

    def _update_causal_mask(
        self,
        attention_mask: torch.Tensor,
        input_tensor: torch.Tensor,
        cache_position: torch.Tensor,
        past_key_values: Cache,
        output_attentions: bool,
    ):
        if self.decoder.config._attn_implementation == "flash_attention_2":
            return attention_mask

        # For SDPA, when possible, we will rely on its `is_causal` argument instead of its `attn_mask` argument, in
        # order to dispatch on Flash Attention 2. This feature is not compatible with static cache, as SDPA will fail
        # to infer the attention mask.
        past_seen_tokens = (
            past_key_values.get_seq_length() if past_key_values is not None else 0
        )

        # We always pass in a 2D attention mask from the processor - In both static and dynamic cache cases
        dtype, device = input_tensor.dtype, input_tensor.device
        min_dtype = torch.finfo(dtype).min
        sequence_length = input_tensor.shape[1]
        target_length = (
            attention_mask.shape[-1]
            if isinstance(attention_mask, torch.Tensor)
            else past_seen_tokens + sequence_length + 1
        )

        # In case the provided `attention` mask is 2D, we generate a causal mask here (4D).
        causal_mask = self._prepare_4d_causal_attention_mask_with_cache_position(
            attention_mask,
            sequence_length=sequence_length,
            target_length=target_length,
            dtype=dtype,
            device=device,
            cache_position=cache_position,
            batch_size=input_tensor.shape[0],
            config=self.config,
            past_key_values=past_key_values,
        )

        if (
            self.config._attn_implementation == "sdpa"
            and attention_mask is not None
            and attention_mask.device.type in ["cuda", "xpu"]
            and not output_attentions
        ):
            # Attend to all tokens in fully masked rows in the causal_mask, for example the relevant first rows when
            # using left padding. This is required by F.scaled_dot_product_attention memory-efficient attention path.
            # Details: https://github.com/pytorch/pytorch/issues/110213
            causal_mask = AttentionMaskConverter._unmask_unattended(
                causal_mask, min_dtype
            )

        return causal_mask

    @staticmethod
    def _prepare_4d_causal_attention_mask_with_cache_position(
        attention_mask: torch.Tensor,
        sequence_length: int,
        target_length: int,
        dtype: torch.dtype,
        device: torch.device,
        cache_position: torch.Tensor,
        batch_size: int,
        config: SuryaModelConfig,
        past_key_values: Cache,
    ):
        """
        Creates a causal 4D mask of shape `(batch_size, 1, query_length, key_value_length)` from a 2D mask of shape
        `(batch_size, key_value_length)`, or if the input `attention_mask` is already 4D, do nothing.

        Args:
            attention_mask (`torch.Tensor`):
                A 2D attention mask of shape `(batch_size, key_value_length)` or a 4D attention mask of shape `(batch_size, 1, query_length, key_value_length)`.
            sequence_length (`int`):
                The sequence length being processed.
            target_length (`int`):
                The target length: when generating with static cache, the mask should be as long as the static cache, to account for the 0 padding, the part of the cache that is not filled yet.
            dtype (`torch.dtype`):
                The dtype to use for the 4D attention mask.
            device (`torch.device`):
                The device to plcae the 4D attention mask on.
            cache_position (`torch.Tensor`):
                Indices depicting the position of the input sequence tokens in the sequence.
            batch_size (`torch.Tensor`):
                Batch size.
            config (`Qwen2Config`):
                The model's configuration class
            past_key_values (`Cache`):
                The cache class that is being used currently to generate
        """
        if attention_mask is not None and attention_mask.dim() == 4:
            # In this case we assume that the mask comes already in inverted form and requires no inversion or slicing.
            causal_mask = attention_mask
        else:
            min_dtype = torch.finfo(dtype).min
            causal_mask = torch.full(
                (sequence_length, target_length),
                fill_value=min_dtype,
                dtype=dtype,
                device=device,
            )
            diagonal_attend_mask = torch.arange(
                target_length, device=device
            ) > cache_position.reshape(-1, 1)
            # NOTE - Removed sliding window handling here from original impl. since we manage it differently
            causal_mask *= diagonal_attend_mask
            causal_mask = causal_mask[None, None, :, :].expand(batch_size, 1, -1, -1)
            if attention_mask is not None:
                causal_mask = (
                    causal_mask.clone()
                )  # copy to contiguous memory for in-place edit
                if attention_mask.shape[-1] > target_length:
                    attention_mask = attention_mask[:, :target_length]
                mask_length = attention_mask.shape[-1]
                padding_mask = causal_mask[:, :, :, :mask_length] + attention_mask[
                    :, None, None, :
                ].to(causal_mask.device)
                padding_mask = padding_mask == 0
                causal_mask[:, :, :, :mask_length] = causal_mask[
                    :, :, :, :mask_length
                ].masked_fill(padding_mask, min_dtype)
        return causal_mask
