"""Hugging Face + PEFT backend for real 32B/70B adapters.

This is intentionally a thin layer: load a frozen base (optionally NF4), attach
LoRA/DoRA, pre-tokenize a JSONL SFT file and expose only the trainable adapter
through the DiLoCo state interface.

For multi-GPU FSDP or massive datasets, treat this as the P2 starting point, not
as the final trainer.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import threading
import torch

from .config import ExperimentConfig
from .inner_optim import AdamW
from .tensor_utils import State, clone_state



def _read_jsonl(path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
            instruction = str(raw.get("instruction", "")).strip()
            output = str(raw.get("output", "")).strip()
            if not instruction or not output:
                continue
            rows.append({"instruction": instruction, "output": output})
    if not rows:
        raise ValueError(f"no usable instruction/output rows in {path}")
    return rows


_MODEL_LOAD_LOCK = threading.Lock()


class HFAdapterBackend:
    def __init__(
        self,
        config: ExperimentConfig,
        shard_id: int = 0,
        device: str = "auto",
    ) -> None:
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer
            from transformers import BitsAndBytesConfig
            from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        except ImportError as exc:
            raise RuntimeError(
                "the hf backend needs transformers, peft and bitsandbytes"
            ) from exc

        self.config = config
        self.shard_id = int(shard_id)
        self.device = torch.device(device)
        self.model_cfg = config.model
        self.train_cfg = config.train
        self._step = 0
        self._epoch = 0
        self._cursor = 0
        self._order: List[int] = []

        model_path = self.model_cfg.name_or_path
        if not model_path:
            raise ValueError("model.name_or_path is required")
        if self.model_cfg.quant == "nf4" and self.device.type != "cuda":
            raise RuntimeError("NF4/QLoRA requires CUDA")

        dtype = self._dtype()
        quant_config = None
        if self.model_cfg.quant == "nf4":
            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=dtype,
                bnb_4bit_use_double_quant=True,
            )

        tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            trust_remote_code=self.model_cfg.trust_remote_code,
        )
        if tokenizer.pad_token_id is None:
            if tokenizer.eos_token_id is None:
                raise ValueError("tokenizer has neither pad_token nor eos_token")
            tokenizer.pad_token = tokenizer.eos_token
        self.tokenizer = tokenizer

        kwargs: Dict[str, Any] = {
            "trust_remote_code": self.model_cfg.trust_remote_code,
            "attn_implementation": self.model_cfg.attn_implementation,
        }
        if quant_config is not None:
            kwargs["quantization_config"] = quant_config
            kwargs["device_map"] = {"": str(self.device)}
            kwargs["low_cpu_mem_usage"] = True
        else:
            kwargs["dtype"] = dtype
            if self.device.type == "cuda":
                kwargs["device_map"] = {"": str(self.device)}
                kwargs["low_cpu_mem_usage"] = True
            else:
                kwargs["low_cpu_mem_usage"] = False
        with _MODEL_LOAD_LOCK:
            try:
                model = AutoModelForCausalLM.from_pretrained(model_path, **kwargs)
            except TypeError:
                kwargs.pop("dtype", None)
                kwargs["torch_dtype"] = dtype
                model = AutoModelForCausalLM.from_pretrained(model_path, **kwargs)
            if self.device.type != "cuda":
                model.to(self.device)
        model.config.use_cache = False

        if quant_config is not None:
            model = prepare_model_for_kbit_training(
                model,
                use_gradient_checkpointing=self.model_cfg.gradient_checkpointing,
            )
        elif self.model_cfg.gradient_checkpointing:
            model.gradient_checkpointing_enable()
            if hasattr(model, "enable_input_require_grads"):
                model.enable_input_require_grads()

        lora_config = LoraConfig(
            r=int(self.model_cfg.rank),
            lora_alpha=int(self.model_cfg.alpha),
            lora_dropout=float(self.model_cfg.lora_dropout),
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=list(self.model_cfg.target_modules),
            use_dora=bool(self.model_cfg.use_dora),
        )
        model = get_peft_model(model, lora_config)
        model.train()
        self.model = model
        self._optimizer = None
        self.reset_optimizer()

        self._train_examples = self._load_examples(
            self.config.data.path,
            self.config.data.max_samples,
            limit_to_shard=True,
        )
        if not self._train_examples:
            raise ValueError(
                f"shard {self.shard_id} has no examples; "
                f"data.num_shards={self.config.data.num_shards}"
            )
        self._eval_examples = []
        if self.config.data.eval_path:
            self._eval_examples = self._load_examples(
                self.config.data.eval_path,
                self.config.data.eval_samples,
                limit_to_shard=False,
            )

    def _dtype(self) -> torch.dtype:
        requested = self.model_cfg.dtype
        if requested == "auto":
            return torch.bfloat16 if self.device.type == "cuda" else torch.float32
        return {
            "bf16": torch.bfloat16,
            "fp16": torch.float16,
            "fp32": torch.float32,
        }[requested]

    def reset_optimizer(self) -> None:
        params = [p for p in self.model.parameters() if p.requires_grad]
        if not params:
            raise RuntimeError("no trainable parameters; LoRA did not attach")
        if self.device.type == "cpu":
            self._optimizer = AdamW(
                params,
                lr=float(self.train_cfg.inner_lr),
                weight_decay=float(self.train_cfg.inner_weight_decay),
            )
        else:
            self._optimizer = torch.optim.AdamW(
                params,
                lr=float(self.train_cfg.inner_lr),
                weight_decay=float(self.train_cfg.inner_weight_decay),
            )

    def _load_examples(
        self,
        path: str,
        limit: int,
        limit_to_shard: bool,
    ) -> List[Dict[str, Any]]:
        rows = _read_jsonl(path)
        if limit_to_shard:
            shards = max(1, int(self.config.data.num_shards))
            rows = [row for index, row in enumerate(rows) if index % shards == self.shard_id]
        if limit > 0:
            rows = rows[:limit]
        examples: List[Dict[str, Any]] = []
        max_length = int(self.model_cfg.max_length)
        eos = self.tokenizer.eos_token_id
        for row in rows:
            if self.config.data.use_chat_template and self.tokenizer.chat_template:
                prompt = self.tokenizer.apply_chat_template(
                    [{"role": "user", "content": row["instruction"]}],
                    tokenize=False,
                    add_generation_prompt=True,
                )
            else:
                prompt = (
                    "### Instruction:\n"
                    f"{row['instruction']}\n\n"
                    "### Response:\n"
                )
            prompt_ids = self.tokenizer(prompt, add_special_tokens=False)["input_ids"]
            answer_ids = self.tokenizer(row["output"], add_special_tokens=False)["input_ids"]
            if eos is not None:
                answer_ids = answer_ids + [int(eos)]
            if len(prompt_ids) >= max_length or not answer_ids:
                continue
            room = max_length - len(prompt_ids)
            answer_ids = answer_ids[:room]
            if not answer_ids:
                continue
            input_ids = list(prompt_ids) + list(answer_ids)
            labels = [-100] * len(prompt_ids) + list(answer_ids)
            examples.append(
                {
                    "input_ids": input_ids,
                    "labels": labels,
                    "attention_mask": [1] * len(input_ids),
                }
            )
        return examples

    def _next_batch(self, round_index: int) -> Dict[str, torch.Tensor]:
        if not self._order or self._cursor >= len(self._order):
            self._epoch += 1
            self._order = list(range(len(self._train_examples)))
            if self.config.data.shuffle:
                random.Random(self.train_cfg.seed + round_index + self._epoch).shuffle(self._order)
            self._cursor = 0
        size = int(self.train_cfg.batch_size)
        indices = [self._order[(self._cursor + idx) % len(self._order)] for idx in range(size)]
        self._cursor += size
        return self._collate([self._train_examples[index] for index in indices])

    def _collate(self, examples: Sequence[Mapping[str, Any]]) -> Dict[str, torch.Tensor]:
        max_len = max(len(example["input_ids"]) for example in examples)
        pad_id = int(self.tokenizer.pad_token_id)
        input_ids: List[List[int]] = []
        labels: List[List[int]] = []
        masks: List[List[int]] = []
        for example in examples:
            pad = max_len - len(example["input_ids"])
            input_ids.append(list(example["input_ids"]) + [pad_id] * pad)
            labels.append(list(example["labels"]) + [-100] * pad)
            masks.append(list(example["attention_mask"]) + [0] * pad)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long, device=self.device),
            "labels": torch.tensor(labels, dtype=torch.long, device=self.device),
            "attention_mask": torch.tensor(masks, dtype=torch.long, device=self.device),
        }

    def train_steps(self, steps: int, round_index: int = 0) -> Dict[str, float]:
        if steps < 1:
            raise ValueError("steps must be >= 1")
        assert self._optimizer is not None
        self.model.train()
        losses: List[float] = []
        tokens = 0.0
        samples = 0.0
        for _ in range(steps):
            self._optimizer.zero_grad(set_to_none=True)
            step_loss = 0.0
            step_tokens = 0.0
            for _ in range(int(self.train_cfg.grad_accum)):
                batch = self._next_batch(round_index)
                output = self.model(**batch)
                loss = output.loss
                (loss / float(self.train_cfg.grad_accum)).backward()
                count = float((batch["labels"] != -100).sum().item())
                step_loss += float(loss.detach().cpu().item()) * count
                step_tokens += count
                samples += float(batch["input_ids"].shape[0])
            if self.train_cfg.max_grad_norm > 0:
                torch.nn.utils.clip_grad_norm_(
                    [p for p in self.model.parameters() if p.requires_grad],
                    float(self.train_cfg.max_grad_norm),
                )
            self._optimizer.step()
            self._step += 1
            if step_tokens > 0:
                losses.append(step_loss / step_tokens)
                tokens += step_tokens
        if not losses:
            raise RuntimeError("empty batches produced no loss")
        mean_loss = sum(losses) / len(losses)
        if not math.isfinite(mean_loss):
            raise FloatingPointError("training produced a non-finite loss")
        return {
            "loss": mean_loss,
            "samples": samples,
            "tokens": tokens,
            "weight": tokens,
        }

    def evaluate(self) -> Dict[str, float]:
        if not self._eval_examples:
            return {}
        was_training = self.model.training
        self.model.eval()
        total_loss = 0.0
        total_tokens = 0.0
        with torch.no_grad():
            for start in range(0, len(self._eval_examples), int(self.train_cfg.batch_size)):
                chunk = self._eval_examples[start : start + int(self.train_cfg.batch_size)]
                batch = self._collate(chunk)
                output = self.model(**batch)
                count = float((batch["labels"] != -100).sum().item())
                total_loss += float(output.loss.detach().cpu().item()) * count
                total_tokens += count
        if was_training:
            self.model.train()
        if total_tokens <= 0:
            return {}
        return {"eval_loss": total_loss / total_tokens}

    def state(self) -> State:
        state: State = {}
        for name, param in self.model.named_parameters():
            if param.requires_grad:
                state[name] = param.detach().to("cpu", torch.float32).contiguous().clone()
        if not state:
            raise RuntimeError("adapter is empty")
        return state

    def load_state(self, state: Mapping[str, torch.Tensor]) -> None:
        current = {
            name: param
            for name, param in self.model.named_parameters()
            if param.requires_grad
        }
        missing = sorted(set(current) - set(state))
        extra = sorted(set(state) - set(current))
        if missing or extra:
            raise ValueError(f"adapter keys differ; missing={missing[:5]} extra={extra[:5]}")
        with torch.no_grad():
            for name, tensor in state.items():
                param = current[name]
                if tuple(param.shape) != tuple(tensor.shape):
                    raise ValueError(
                        f"shape mismatch for {name}: {tuple(param.shape)} != {tuple(tensor.shape)}"
                    )
                param.copy_(tensor.detach().to(param.device, param.dtype))

    def base_fingerprint(self) -> str:
        raw_config = self.model.config.to_dict()
        payload = {
            "name_or_path": self.model_cfg.name_or_path,
            "quant": self.model_cfg.quant,
            "dtype": self.model_cfg.dtype,
            "config": raw_config,
        }
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def adapter_meta(self) -> Dict[str, Any]:
        state = self.state()
        total = sum(p.numel() for p in self.model.parameters())
        trainable = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
        return {
            "kind": "hf",
            "rank": int(self.model_cfg.rank),
            "alpha": float(self.model_cfg.alpha),
            "quant": self.model_cfg.quant,
            "target_modules": list(self.model_cfg.target_modules),
            "state_keys": sorted(state),
            "state_bytes": int(sum(v.numel() * v.element_size() for v in state.values())),
            "total_params": int(total),
            "trainable_params": int(trainable),
            "base_hash": self.base_fingerprint(),
        }

    def close(self) -> None:
        self._optimizer = None
        self.model = None
        self.tokenizer = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
