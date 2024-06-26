from __future__ import annotations

import torch
from torch import Tensor
from torch.nn import Module, Parameter
from torch.nn.functional import linear
from torch.nn.utils.rnn import pad_sequence

from math import ceil, log2
from typing import NoReturn

from .schemes import grid_applicative, applicative, AtnFn


class UnitarySequential(Module):
    def __init__(self, dim: int, num_heads: int) -> None:
        super(UnitarySequential, self).__init__()
        self.dim = dim
        self.num_heads = num_heads
        self._primitives = Parameter(
            torch.tril(torch.randn(self.num_heads, self.dim, self.dim), diagonal=-1).softmax(dim=-1))
        self.maps = None

    @property
    def hermitian(self) -> Tensor:
        primitives = self._primitives * torch.tril(torch.ones_like(self._primitives), diagonal=-1)
        return primitives - primitives.mT

    @property
    def primitives(self) -> Tensor:
        hermitian = self.hermitian
        return torch.matrix_exp(hermitian)

    def forward(self, position_ids: Tensor) -> Tensor:
        return self.maps[position_ids]

    @staticmethod
    def adjust_attention(q_maps: Tensor, k_maps: Tensor, mediator: tuple[Tensor, bool] | None) -> AtnFn:
        return applicative(q_maps, k_maps, mediator=mediator)

    def _make_maps(self, size: int) -> Tensor:
        def expand(history: Tensor) -> Tensor:
            longest = history[-1]
            expanded = history @ longest
            return torch.cat((history, expanded), dim=0)

        maps = self.primitives.unsqueeze(0)
        for _ in range(ceil(log2(size))):
            maps = expand(maps)
        maps = maps[:size]
        eye = torch.eye(self.dim, device=self.primitives.device)[None].repeat(self.num_heads, 1, 1)
        return torch.cat(
            (eye[None],
             maps))

    def precompute(self, size: int) -> None:
        self.maps = self._make_maps(size)


def create_paths(
        max_depth: int,
        branching_factor: int) -> list[list[int]]:
    paths = [[]]
    for node_idx in range(1, branching_factor ** max_depth):
        root_idx = (node_idx - 1) // branching_factor
        branch_idx = (node_idx - 1) % branching_factor
        path = paths[root_idx] + [branch_idx]
        paths.append(path)
    return [[branching_factor + 1]] * 2 + paths


def create_steps(path_words: Tensor, branching_factor: int) -> Tensor:
    point_mask = path_words.ne(branching_factor + 1)
    mask = point_mask[:, None] & point_mask[None]
    pointwise_equal = path_words[:, None].eq(path_words[None])
    common_prefix = pointwise_equal.cumprod(-1).logical_and(mask)
    sum_lens = point_mask.sum(-1)[:, None] + point_mask.sum(-1)[None]
    cpl = common_prefix.sum(-1)
    return sum_lens - 2 * cpl


class UnitaryBranching(Module):
    def __init__(self, dim: int, branching_factor: int, num_heads: int):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.branching_factor = branching_factor
        self.identity: Parameter = Parameter(torch.eye(dim)[None, None], requires_grad=False)
        self._primitives = Parameter(
            torch.rand(self.branching_factor * self.num_heads + 1, self.dim, self.dim).softmax(dim=-1).cumsum(dim=-1))
        self.maps = None
        self.paths = create_paths(16, self.branching_factor)

    @property
    def hermitian(self) -> Tensor:
        return self._primitives - self._primitives.mH

    @property
    def primitives(self) -> Tensor:
        hermitian = self.hermitian
        return torch.matrix_exp(hermitian)

    def forward(self, mapping: Tensor) -> NoReturn:
        raise NotImplementedError('You have to index the precomputed maps by hand')

    @staticmethod
    def adjust_attention(q_maps: Tensor, k_maps: Tensor, mediator: tuple[Tensor, bool] | None) -> AtnFn:
        return applicative(q_maps, k_maps, mediator=mediator)

    def precompute(self, positions: list[int]) -> None:
        self.maps = self.embed_positions(positions)

    def embed_positions(self, positions: list[int]) -> tuple[Tensor, Tensor]:
        primitives = self.primitives
        path_words = pad_sequence(
            sequences=[
                torch.tensor(self.paths[pos + 2], device=self.primitives.device, dtype=torch.long)
                if pos > 0 else torch.empty(0, device=self.primitives.device, dtype=torch.long)
                for pos in positions], padding_value=self.branching_factor, batch_first=True
        )
        steps = create_steps(path_words, self.branching_factor)

        maps = self.identity.repeat(len(positions), 1, 1, 1)

        masks = [path_words == branch for branch in range(self.branching_factor)]

        for step in range(path_words.size(1)):
            for branch, mask in enumerate(masks):
                maps[mask[:, step]] = linear(maps[mask[:, step]], primitives[branch])
        return maps, steps


class UnitaryGrid(Module):
    def __init__(self, num_axes: int, dim: int, num_heads: int) -> None:
        super(UnitaryGrid, self).__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.num_axes = num_axes
        self._primitives = Parameter(
            torch.rand(self.num_axes * self.num_heads, self.dim, self.dim).softmax(dim=-1).cumsum(-1))
        self.maps = None

    @property
    def hermitian(self) -> Tensor:
        return self._primitives - self._primitives.mH

    @property
    def primitives(self) -> Tensor:
        hermitian = self.hermitian
        return torch.matrix_exp(hermitian)

    def forward(self, xs: Tensor, ys: Tensor) -> tuple[Tensor, Tensor]:
        maps_x, maps_y = self.maps.chunk(2, dim=1)
        maps_x = maps_x.squeeze(1)
        maps_y = maps_y.squeeze(1)
        return maps_x[xs], maps_y[ys]

    @staticmethod
    def adjust_attention(
            q_maps: tuple[Tensor, Tensor],
            k_maps: tuple[Tensor, Tensor],
            mediator: tuple[Tensor, bool] | None) -> AtnFn:
        return grid_applicative(q_maps, k_maps, mediator=mediator)

    def _make_maps(self, size: int) -> Tensor:
        def expand(history: Tensor) -> Tensor:
            longest = history[-1]
            expanded = history @ longest
            return torch.cat((history, expanded), dim=0)

        maps = self.primitives.unsqueeze(0)
        for _ in range(ceil(log2(size))):
            maps = expand(maps)
        maps = maps[:size]
        eye = torch.eye(self.dim, device=self.primitives.device)[None].repeat(self.num_axes * self.num_heads, 1, 1)
        maps = torch.cat((eye[None], maps))
        return maps.view(-1, self.num_axes, self.num_heads, self.dim, self.dim)

    def precompute(self, size: int) -> None:
        self.maps = self._make_maps(size)
