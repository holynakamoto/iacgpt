#!/usr/bin/env python3
"""
TPU-compatible training script using PyTorch XLA.
Based on base_train.py with XLA-specific modifications.
"""

import os
import sys
import math
import time
import argparse
from pathlib import Path

import torch
import torch.nn.functional as F

# TPU/XLA imports
try:
    import torch_xla.core.xla_model as xm
    import torch_xla.distributed.parallel_loader as pl
    import torch_xla.distributed.xla_multiprocessing as xmp
    TPU_AVAILABLE = True
except ImportError:
    TPU_AVAILABLE = False
    print("WARNING: torch_xla not installed. TPU training unavailable.")

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from gpt import GPT, GPTConfig
from dataloader import DistributedDataLoader
from optim import configure_optimizers
from common import print0, get_ddp_config
import checkpoint_manager as ckpt


def train_on_tpu(rank, args):
    """
    Training function for a single TPU core.
    Called by xmp.spawn() for distributed TPU training.
    """
    # TPU-specific setup
    device = xm.xla_device()
    world_size = xm.xrt_world_size()

    print0(f"TPU Training on core {rank}/{world_size}")
    print0(f"Device: {device}")

    # Load tokenizer
    from tokenizer import Tokenizer
    tokenizer = Tokenizer()
    vocab_size = tokenizer.vocab_size

    # Create model
    config = GPTConfig(
        vocab_size=vocab_size,
        depth=args.depth,
        T=args.context_length,
        window_pattern=args.window_pattern,
    )
    model = GPT(config).to(device)

    print0(f"Model: {config.depth} layers, {config.d_model} dim, {sum(p.numel() for p in model.parameters())/1e6:.1f}M params")

    # Dataloader
    data_loader = DistributedDataLoader(
        shard_dir="~/.cache/nanochat/base_data",
        batch_size=args.device_batch_size,
        context_length=args.context_length,
        world_size=world_size,
        rank=rank,
    )

    # Calculate training iterations
    total_tokens = args.target_param_data_ratio * sum(p.numel() for p in model.parameters())
    total_batch_size = args.device_batch_size * args.context_length * world_size * args.grad_accum_steps
    num_iterations = int(total_tokens / total_batch_size)

    print0(f"Training for {num_iterations} iterations ({total_tokens/1e9:.2f}B tokens)")

    # Optimizer
    optimizer = configure_optimizers(
        model,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        device_type="xla",
    )

    # Training loop
    model.train()
    step = 0
    tokens_seen = 0

    for epoch in range(100):  # Max epochs
        data_loader.reset()

        for micro_step in range(num_iterations):
            t0 = time.time()

            # Gradient accumulation
            loss_accum = 0.0
            for grad_step in range(args.grad_accum_steps):
                x, y = data_loader.next_batch()
                x, y = x.to(device), y.to(device)

                # Forward pass
                with torch.cuda.amp.autocast(dtype=torch.bfloat16):
                    loss = model(x, y)

                # Backward pass (scaled for accumulation)
                loss = loss / args.grad_accum_steps
                loss.backward()
                loss_accum += loss.detach()

                tokens_seen += x.numel()

            # XLA-specific: Reduce loss across TPU cores
            loss_accum = xm.all_reduce(xm.REDUCE_SUM, loss_accum) / world_size

            # XLA-specific: Optimizer step
            xm.optimizer_step(optimizer)
            optimizer.zero_grad()

            # XLA-specific: Mark step to execute graph
            xm.mark_step()

            step += 1
            t1 = time.time()
            dt = t1 - t0
            tokens_per_sec = (args.device_batch_size * args.context_length * args.grad_accum_steps * world_size) / dt

            if step % 10 == 0:
                print0(f"step {step:5d} | loss {loss_accum.item():.4f} | dt {dt*1000:.0f}ms | tok/sec {tokens_per_sec:.0f}")

            # Checkpointing
            if step % args.save_every == 0 and rank == 0:
                ckpt_path = f"~/.cache/nanochat/tpu_checkpoints/iac-gpt-d{args.depth}"
                os.makedirs(ckpt_path, exist_ok=True)

                # Save model checkpoint
                torch.save({
                    'step': step,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'loss': loss_accum.item(),
                }, f"{ckpt_path}/model_{step:06d}.pt")

                print0(f"✅ Saved checkpoint at step {step}")

            if step >= num_iterations:
                print0(f"Training complete! {tokens_seen/1e9:.2f}B tokens seen")
                return


def main():
    parser = argparse.ArgumentParser(description="Train IaC-GPT on TPU")
    parser.add_argument("--depth", type=int, default=12, help="Model depth (number of layers)")
    parser.add_argument("--device-batch-size", type=int, default=4, help="Batch size per TPU core")
    parser.add_argument("--context-length", type=int, default=4096, help="Context length")
    parser.add_argument("--window-pattern", type=str, default="L", help="Attention window pattern")
    parser.add_argument("--target-param-data-ratio", type=int, default=8, help="Tokens per parameter ratio")
    parser.add_argument("--grad-accum-steps", type=int, default=4, help="Gradient accumulation steps")
    parser.add_argument("--learning-rate", type=float, default=3e-4, help="Learning rate")
    parser.add_argument("--weight-decay", type=float, default=0.1, help="Weight decay")
    parser.add_argument("--save-every", type=int, default=100, help="Save checkpoint every N steps")
    parser.add_argument("--run", type=str, default="dummy", help="WandB run name")
    parser.add_argument("--model-tag", type=str, default=None, help="Model tag for checkpointing")

    args = parser.parse_args()

    if not TPU_AVAILABLE:
        print("ERROR: torch_xla not installed. Install with: pip install torch-xla")
        sys.exit(1)

    # Launch distributed training on TPU
    # xmp.spawn() will call train_on_tpu() on each TPU core
    xmp.spawn(train_on_tpu, args=(args,), nprocs=8)


if __name__ == "__main__":
    main()
