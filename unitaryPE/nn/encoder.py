from torch.nn import Module, ModuleList, Sequential, Linear, ReLU, GELU, LayerNorm, Dropout
from torch import Tensor
from .attention import SelfMHA, AtnFn
from .drop_path import DropPath

from typing import Literal


def _get_activation(name: Literal['ReLU', 'GELU']) -> Module:
    match name:
        case 'ReLU':
            return ReLU()
        case 'GELU':
            return GELU()
        case _:
            raise ValueError


class EncoderLayer(Module):
    def __init__(
            self,
            num_heads: int,
            dim: int,
            dropout_rate: float,
            weight_dropout: float,
            drop_path: bool,
            mlp_ratio: int,
            activation: Literal['ReLU', 'GELU']) -> None:
        super(EncoderLayer, self).__init__()
        self.mha = SelfMHA(num_heads, dim, dropout_rate=weight_dropout)
        self.ffn = Sequential(Linear(dim, mlp_ratio * dim), _get_activation(activation), Linear(mlp_ratio * dim, dim))
        self.mha_ln = LayerNorm(dim)
        self.ffn_ln = LayerNorm(dim)
        self.dropout = (DropPath if drop_path else Dropout)(dropout_rate)

    def forward(
            self,
            encoder_input: Tensor,
            encoder_mask: Tensor,
            atn_fn: AtnFn) -> Tensor:
        mha = self.mha_ln(encoder_input)
        mha = self.mha.forward(mha, encoder_mask, atn_fn)
        mha = self.dropout(mha)
        mha = mha + encoder_input
        ffn = self.ffn_ln.forward(mha)
        ffn = self.ffn(ffn)
        ffn = self.dropout(ffn)
        return ffn + mha


class Encoder(Module):
    def __init__(
            self,
            num_heads: int,
            num_layers: int,
            dim: int,
            dropout_rate: float = 0.15,
            weight_dropout: float = 0.,
            mlp_ratio: int = 4,
            activation: Literal['ReLU', 'GELU'] = 'ReLU',
            drop_path: bool = False) -> None:
        super(Encoder, self).__init__()
        self.encoder_layers = ModuleList(
            [EncoderLayer(
                num_heads=num_heads,
                dim=dim,
                dropout_rate=dropout_rate * depth if drop_path else dropout_rate,
                weight_dropout=weight_dropout,
                mlp_ratio=mlp_ratio,
                activation=activation,
                drop_path=drop_path)
             for depth in range(num_layers)])

    def forward(
            self,
            encoder_input: Tensor,
            encoder_mask: Tensor,
            atn_fn: AtnFn) -> Tensor:
        for layer in self.encoder_layers:
            encoder_input = layer.forward(
                encoder_input=encoder_input,
                encoder_mask=encoder_mask,
                atn_fn=atn_fn)
        return encoder_input
