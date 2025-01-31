import os
import shutil
from os.path import exists
import random

import torch
import torch.nn as nn
from torch.nn.functional import log_softmax, pad
import math
import copy
import time
from torch.optim.lr_scheduler import LambdaLR
import pandas as pd
import altair as alt
import matplotlib.pyplot as plt
import sys

from torchtext.data.functional import to_map_style_dataset
from torch.utils.data import DataLoader
from torchtext.vocab import build_vocab_from_iterator
import torchtext.datasets as datasets
import spacy
import GPUtil
import warnings
from torch.utils.data.distributed import DistributedSampler
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP

device = 'cuda' if torch.cuda.is_available() else 'cpu'

operators = ['~', '^', '$', '+', '-', '=']  # blank, start,end, plus, minus
start_symbol = 1
pad_symbol = 0
voca_size = len(operators) + ord('9') - ord('0') + 1 + ord('J') - ord('A') + 1
operators_function = [None, None, lambda x, y: x + y, lambda x, y: x - y, None]


def to_token(ch):
    if ch in operators:
        return operators.index(ch)
    else:
        if ord(ch) > ord('9'):
            res = int(ord(ch) - ord('A') + 10) + len(operators)
        else:
            res = int(ord(ch) - ord('0')) + len(operators)
        assert res < voca_size
        return res


def from_token(token):
    if token < len(operators):
        return operators[token]
    elif token > to_token('9'):
        return chr(token - to_token('9') + ord('A') - 1)
    else:
        return chr(ord('0') + token - len(operators))


def to_tokens(text):
    res = []
    for ch in text:
        token = to_token(ch)
        res.append(token)
    return res


def from_tokens(tokens):
    res = ""
    for token in tokens:
        res += from_token(token)
    return res


# device="cpu"

def clones(module, N):
    "Produce N identical layers."
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])


class EncoderDecoder(nn.Module):
    """
    A standard Encoder-Decoder architecture. Base for this and many
    other models.
    """

    def __init__(self, encoder, decoder, src_embed, tgt_embed, generator):
        super(EncoderDecoder, self).__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.src_embed = src_embed
        self.tgt_embed = tgt_embed
        self.generator = generator

    def forward(self, src, tgt, src_mask, tgt_mask):
        "Take in and process masked src and target sequences."
        return self.decode(self.encode(src, src_mask), src_mask, tgt, tgt_mask)

    def encode(self, src, src_mask):
        return self.encoder(self.src_embed(src), src_mask)

    def decode(self, memory, src_mask, tgt, tgt_mask):
        return self.decoder(self.tgt_embed(tgt), memory, src_mask, tgt_mask)


class Generator(nn.Module):
    "Define standard linear + softmax generation step."

    def __init__(self, d_model, vocab):
        super(Generator, self).__init__()
        self.proj = nn.Linear(d_model, vocab)

    def forward(self, x):
        return log_softmax(self.proj(x), dim=-1)


class LayerNorm(nn.Module):
    "Construct a layernorm module (See citation for details)."

    def __init__(self, features, eps=1e-6):
        super(LayerNorm, self).__init__()
        self.a_2 = nn.Parameter(torch.ones(features))
        self.b_2 = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.a_2 * (x - mean) / (std + self.eps) + self.b_2


class Encoder(nn.Module):
    "Core encoder is a stack of N layers"

    def __init__(self, layer, N):
        super(Encoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)

    def forward(self, x, mask):
        "Pass the input (and mask) through each layer in turn."
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)


class SublayerConnection(nn.Module):
    """
    A residual connection followed by a layer norm.
    Note for code simplicity the norm is first as opposed to last.
    """

    def __init__(self, size, dropout):
        super(SublayerConnection, self).__init__()
        self.norm = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, sublayer):
        "Apply residual connection to any sublayer with the same size."
        return x + self.dropout(sublayer(self.norm(x)))


class EncoderLayer(nn.Module):
    "Encoder is made up of self-attn and feed forward (defined below)"

    def __init__(self, size, self_attn, feed_forward, dropout):
        super(EncoderLayer, self).__init__()
        self.self_attn = self_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size, dropout), 2)
        self.size = size

    def forward(self, x, mask):
        "Follow Figure 1 (left) for connections."
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mask))
        return self.sublayer[1](x, self.feed_forward)


class Decoder(nn.Module):
    "Generic N layer decoder with masking."

    def __init__(self, layer, N):
        super(Decoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)

    def forward(self, x, memory, src_mask, tgt_mask):
        for layer in self.layers:
            x = layer(x, memory, src_mask, tgt_mask)
        return self.norm(x)


class DecoderLayer(nn.Module):
    "Decoder is made of self-attn, src-attn, and feed forward (defined below)"

    def __init__(self, size, self_attn, src_attn, feed_forward, dropout):
        super(DecoderLayer, self).__init__()
        self.size = size
        self.self_attn = self_attn
        self.src_attn = src_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size, dropout), 3)

    def forward(self, x, memory, src_mask, tgt_mask):
        "Follow Figure 1 (right) for connections."
        m = memory
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, tgt_mask))
        x = self.sublayer[1](x, lambda x: self.src_attn(x, m, m, src_mask))
        return self.sublayer[2](x, self.feed_forward)


def subsequent_mask(size):
    "Mask out subsequent positions."
    attn_shape = (1, size, size)
    subsequent_mask = torch.triu(torch.ones(attn_shape), diagonal=1).type(
        torch.uint8
    )
    return subsequent_mask == 0


def attention(query, key, value, mask=None, dropout=None):
    "Compute 'Scaled Dot Product Attention'"
    d_k = query.size(-1)
    scores = torch.matmul(query, key.transpose(-2, -1)) / math.sqrt(d_k)
    if mask is not None:
        scores = scores.masked_fill(mask == 0, -1e9)
    p_attn = scores.softmax(dim=-1)
    if dropout is not None:
        p_attn = dropout(p_attn)
    return torch.matmul(p_attn, value), p_attn


class MultiHeadedAttention(nn.Module):
    def __init__(self, h, d_model, dropout=0.1):
        "Take in model size and number of heads."
        super(MultiHeadedAttention, self).__init__()
        assert d_model % h == 0
        # We assume d_v always equals d_k
        self.d_k = d_model // h
        self.h = h
        self.linears = clones(nn.Linear(d_model, d_model), 4)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, query, key, value, mask=None):
        "Implements Figure 2"
        if mask is not None:
            # Same mask applied to all h heads.
            mask = mask.unsqueeze(1)
        nbatches = query.size(0)

        # 1) Do all the linear projections in batch from d_model => h x d_k
        query, key, value = [
            lin(x).view(nbatches, -1, self.h, self.d_k).transpose(1, 2)
            for lin, x in zip(self.linears, (query, key, value))
        ]

        # 2) Apply attention on all the projected vectors in batch.
        x, self.attn = attention(
            query, key, value, mask=mask, dropout=self.dropout
        )

        # 3) "Concat" using a view and apply a final linear.
        x = (
            x.transpose(1, 2)
            .contiguous()
            .view(nbatches, -1, self.h * self.d_k)
        )
        del query
        del key
        del value
        return self.linears[-1](x)


class PositionwiseFeedForward(nn.Module):
    "Implements FFN equation."

    def __init__(self, d_model, d_ff, dropout=0.1):
        super(PositionwiseFeedForward, self).__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.w_2(self.dropout(self.w_1(x).relu()))


class Embeddings(nn.Module):
    def __init__(self, d_model, vocab):
        super(Embeddings, self).__init__()
        self.lut = nn.Embedding(vocab, d_model)
        self.d_model = d_model

    def forward(self, x):
        return self.lut(x) * math.sqrt(self.d_model)


class PositionalEncoding(nn.Module):
    "Implement the PE function."

    def __init__(self, d_model, dropout, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        # Compute the positional encodings once in log space.
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * -(math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.register_buffer("pe", pe)

    def forward(self, x):
        x = x + self.pe[:, : x.size(1)].requires_grad_(False)
        return self.dropout(x)


def make_model(
        src_vocab, tgt_vocab, N=6, d_model=512, d_ff=2048, h=8, dropout=0.1
):
    "Helper: Construct a model from hyperparameters."
    c = copy.deepcopy
    attn = MultiHeadedAttention(h, d_model)
    ff = PositionwiseFeedForward(d_model, d_ff, dropout)
    position = PositionalEncoding(d_model, dropout)
    model = EncoderDecoder(
        Encoder(EncoderLayer(d_model, c(attn), c(ff), dropout), N),
        Decoder(DecoderLayer(d_model, c(attn), c(attn), c(ff), dropout), N),
        nn.Sequential(Embeddings(d_model, src_vocab), c(position)),
        nn.Sequential(Embeddings(d_model, tgt_vocab), c(position)),
        Generator(d_model, tgt_vocab),
    )

    # This was important from their code.
    # Initialize parameters with Glorot / fan_avg.
    for p in model.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)
    return model


def inference_test():
    test_model = make_model(11, 11, 2)
    test_model.eval()
    src = torch.LongTensor([[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]])
    src_mask = torch.ones(1, 1, 10)

    memory = test_model.encode(src, src_mask)
    ys = torch.zeros(1, 1).type_as(src)

    for i in range(9):
        out = test_model.decode(
            memory, src_mask, ys, subsequent_mask(ys.size(1)).type_as(src.data)
        )
        prob = test_model.generator(out[:, -1])
        _, next_word = torch.max(prob, dim=1)
        next_word = next_word.data[0]
        ys = torch.cat(
            [ys, torch.empty(1, 1).type_as(src.data).fill_(next_word)], dim=1
        )

    print("Example Untrained Model Prediction:", ys)


def run_tests():
    for _ in range(10):
        inference_test()


class Batch:
    """Object for holding a batch of data with mask during training."""

    def __init__(self, src, tgt=None, pad=pad_symbol):  # 0 = <blank>
        src = src.to(device=device)
        self.src = src
        self.src_mask = (src != pad).unsqueeze(-2)
        if tgt is not None:
            tgt = tgt.to(device=device)
            self.tgt = tgt[:, :-1]
            self.tgt_y = tgt[:, 1:]
            self.tgt_mask = self.make_std_mask(self.tgt, pad)
            self.ntokens = (self.tgt_y != pad).data.sum()

    @staticmethod
    def make_std_mask(tgt, pad):
        "Create a mask to hide padding and future words."
        tgt_mask = (tgt != pad).unsqueeze(-2)
        tgt_mask = tgt_mask & subsequent_mask(tgt.size(-1)).type_as(
            tgt_mask.data
        )
        return tgt_mask


class TrainState:
    """Track number of steps, examples, and tokens processed"""

    step: int = 0  # Steps in the current epoch
    accum_step: int = 0  # Number of gradient accumulation steps
    samples: int = 0  # total # of examples used
    tokens: int = 0  # total # of tokens processed


# %% id="2HAZD3hiTsqJ"
def run_epoch(
        data_iter,
        model,
        loss_compute,
        optimizer,
        scheduler,
        mode="train",
        accum_iter=1,
        train_state=TrainState(),
):
    """Train a single epoch"""
    start = time.time()
    total_tokens = 0
    total_loss = 0
    tokens = 0
    n_accum = 0
    for i, batch in enumerate(data_iter):
        out = model.forward(
            batch.src, batch.tgt, batch.src_mask, batch.tgt_mask
        )
        loss, loss_node = loss_compute(out, batch.tgt_y, batch.ntokens)
        # loss_node = loss_node / accum_iter
        if mode == "train" or mode == "train+log":
            loss_node.backward()
            train_state.step += 1
            train_state.samples += batch.src.shape[0]
            train_state.tokens += batch.ntokens
            if i % accum_iter == 0:
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
                n_accum += 1
                train_state.accum_step += 1
            scheduler.step()

        total_loss += loss
        total_tokens += batch.ntokens
        tokens += batch.ntokens
        if i % 40 == 1 and (mode == "train" or mode == "train+log"):
            lr = optimizer.param_groups[0]["lr"]
            elapsed = time.time() - start
            print(
                (
                        "Epoch Step: %6d | Accumulation Step: %3d | Loss: %6.6f "
                        + "| Tokens / Sec: %7.1f | Learning Rate: %6.1e"
                )
                % (i, n_accum, loss / batch.ntokens, tokens / elapsed, lr)
            )
            start = time.time()
            tokens = 0
        del loss
        del loss_node
    return total_loss / total_tokens, train_state


def rate(step, model_size, factor, warmup):
    """
    we have to default the step to 1 for LambdaLR function
    to avoid zero raising to negative power.
    """
    if step == 0:
        step = 1
    return factor * (
            model_size ** (-0.5) * min(step ** (-0.5), step * warmup ** (-1.5))
    )


# %% id="l1bnrlnSV8J5" tags=[]
def example_learning_schedule():
    opts = [
        [512, 1, 4000],  # example 1
        [512, 1, 8000],  # example 2
        [256, 1, 4000],  # example 3
    ]

    dummy_model = torch.nn.Linear(1, 1)
    learning_rates = []
    # we have 3 examples in opts list.
    for idx, example in enumerate(opts):
        # run 20000 epoch for each example
        optimizer = torch.optim.Adam(
            dummy_model.parameters(), lr=1, betas=(0.9, 0.98), eps=1e-9
        )
        lr_scheduler = LambdaLR(
            optimizer=optimizer, lr_lambda=lambda step: rate(step, *example)
        )
        tmp = []
        # take 20K dummy training steps, save the learning rate at each step
        for step in range(20000):
            tmp.append(optimizer.param_groups[0]["lr"])
            optimizer.step()
            lr_scheduler.step()
        learning_rates.append(tmp)

    learning_rates = torch.tensor(learning_rates)

    # Enable altair to handle more than 5000 rows
    alt.data_transformers.disable_max_rows()

    opts_data = pd.concat(
        [
            pd.DataFrame(
                {
                    "Learning Rate": learning_rates[warmup_idx, :],
                    "model_size:warmup": ["512:4000", "512:8000", "256:4000"][
                        warmup_idx
                    ],
                    "step": range(20000),
                }
            )
            for warmup_idx in [0, 1, 2]
        ]
    )

    return (
        alt.Chart(opts_data)
        .mark_line()
        .properties(width=600)
        .encode(x="step", y="Learning Rate", color="model_size:warmup:N")
        .interactive()
    )


class LabelSmoothing(nn.Module):
    "Implement label smoothing."

    def __init__(self, size, padding_idx, smoothing=0.0):
        super(LabelSmoothing, self).__init__()
        self.criterion = nn.KLDivLoss(reduction="sum")
        self.padding_idx = padding_idx
        self.confidence = 1.0 - smoothing
        self.smoothing = smoothing
        self.size = size
        self.true_dist = None

    def forward(self, x, target):
        assert x.size(1) == self.size
        true_dist = x.data.clone()
        true_dist.fill_(self.smoothing / (self.size - 2))
        true_dist.scatter_(1, target.data.unsqueeze(1), self.confidence)
        true_dist[:, self.padding_idx] = 0
        mask = torch.nonzero(target.data == self.padding_idx)
        if mask.dim() > 0:
            true_dist.index_fill_(0, mask.squeeze(), 0.0)
        self.true_dist = true_dist
        return self.criterion(x, true_dist.clone().detach())


def example_label_smoothing():
    crit = LabelSmoothing(5, 0, 0.4)
    predict = torch.FloatTensor(
        [
            [0, 0.2, 0.7, 0.1, 0],
            [0, 0.2, 0.7, 0.1, 0],
            [0, 0.2, 0.7, 0.1, 0],
            [0, 0.2, 0.7, 0.1, 0],
            [0, 0.2, 0.7, 0.1, 0],
        ]
    )
    crit(x=predict.log(), target=torch.LongTensor([2, 1, 0, 3, 3]))
    LS_data = pd.concat(
        [
            pd.DataFrame(
                {
                    "target distribution": crit.true_dist[x, y].flatten(),
                    "columns": y,
                    "rows": x,
                }
            )
            for y in range(5)
            for x in range(5)
        ]
    )

    return (
        alt.Chart(LS_data)
        .mark_rect(color="Blue", opacity=1)
        .properties(height=200, width=200)
        .encode(
            alt.X("columns:O", title=None),
            alt.Y("rows:O", title=None),
            alt.Color(
                "target distribution:Q", scale=alt.Scale(scheme="viridis")
            ),
        )
        .interactive()
    )


def loss(x, crit):
    d = x + 3 * 1
    predict = torch.FloatTensor([[0.00001, x / d, 1 / d, 1 / d, 1 / d]])
    return crit(predict.log(), torch.LongTensor([1])).data


def penalization_visualization():
    crit = LabelSmoothing(5, 0, 0.1)
    loss_value = [loss(x, crit) for x in range(1, 100)]
    loss_data = pd.DataFrame(
        {
            "Loss": loss_value,
            "Steps": list(range(99)),
        }
    ).astype("float")

    plt.plot(loss_value)
    plt.show()


def data_gen(V, batch_size, nbatches):
    "Generate random data for a src-tgt copy task."
    for i in range(nbatches):
        data = torch.randint(1, V, size=(batch_size, 10))
        data[:, 0] = 1
        src = data.requires_grad_(False).clone().detach()
        tgt = data.requires_grad_(False).clone().detach()
        yield Batch(src, tgt, 0)


class SimpleLossCompute:
    "A simple loss compute and train function."

    def __init__(self, generator, criterion):
        self.generator = generator
        self.criterion = criterion

    def __call__(self, x, y, norm):
        x = self.generator(x)
        sloss = (
                self.criterion(
                    x.contiguous().view(-1, x.size(-1)), y.contiguous().view(-1)
                )
                / norm
        )
        return sloss.data * norm, sloss


def greedy_decode(model, src, src_mask, max_len, start_symbol, pad=pad_symbol):
    memory = model.encode(src, src_mask)
    ys = torch.zeros(1, 1).fill_(start_symbol).type_as(src.data)
    for i in range(max_len - 1):
        ys_mask = (ys != pad)
        ys_mask = ys_mask & subsequent_mask(ys.size(-1)).type_as(
            ys_mask.data
        )
        out = model.decode(
            memory, src_mask, ys, ys_mask
        )
        prob = model.generator(out)
        _, next_word = torch.max(prob[:, -1], dim=1)
        next_word = next_word.data[0]
        ys = torch.cat(
            [ys, torch.zeros(1, 1).type_as(src.data).fill_(next_word)], dim=1
        )
    return ys


class DummyOptimizer(torch.optim.Optimizer):
    def __init__(self):
        self.param_groups = [{"lr": 0}]
        None

    def step(self):
        None

    def zero_grad(self, set_to_none=False):
        None


class DummyScheduler:
    def step(self):
        None


def example_simple_model():
    V = 11
    criterion = LabelSmoothing(size=V, padding_idx=0, smoothing=0.0)
    model = make_model(V, V, N=2)

    optimizer = torch.optim.Adam(
        model.parameters(), lr=0.5, betas=(0.9, 0.98), eps=1e-9
    )
    lr_scheduler = LambdaLR(
        optimizer=optimizer,
        lr_lambda=lambda step: rate(
            step, model_size=model.src_embed[0].d_model, factor=1.0, warmup=400
        ),
    )

    batch_size = 80
    for epoch in range(20):
        model.train()
        run_epoch(
            data_gen(V, batch_size, 20),
            model,
            SimpleLossCompute(model.generator, criterion),
            optimizer,
            lr_scheduler,
            mode="train",
        )
        model.eval()
        run_epoch(
            data_gen(V, batch_size, 5),
            model,
            SimpleLossCompute(model.generator, criterion),
            DummyOptimizer(),
            DummyScheduler(),
            mode="eval",
        )[0]

    model.eval()
    src = torch.LongTensor([[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]])
    max_len = src.shape[1]
    src_mask = torch.ones(1, 1, max_len)
    print(greedy_decode(model, src, src_mask, max_len=max_len, start_symbol=0))


def padding_batch(batch):
    length = max(list(map(len, batch)))
    res = []
    for item in batch:
        res.append([to_token('^')] + item + [to_token('$')] + [pad_symbol] * (length - len(item)))
    return res


def generate_input_batch(text, padding=""):
    res = [to_token('^')]
    res += to_tokens(text) + [to_token('$')] + [pad_symbol for ch in padding]
    return torch.tensor([res])


def generate_one_pair(left1, left2, operator):
    if isinstance(operator, str):
        operator_index = operators.index(operator)
    else:
        operator_index = operator
        operator = operators[operator_index]
    target = operators_function[operator_index](left1, left2)
    text = f"{''.join(list(reversed(str(left1))))}{operator}{''.join(list(reversed(str(left2))))}="
    target_text = f"{''.join(list(reversed(str(target))))}"
    return text, target_text


def padding_str(text, length, position='left'):
    if position == 'left':
        if length > len(text):
            text = '0' * (length - len(text)) + text
    elif position == 'right':
        if length > len(text):
            text = text + '0' * (length - len(text))
    return text


def sum_two_str(str1, str2):
    length = max(len(str1), len(str2))
    text1 = padding_str(str1, length)
    text2 = padding_str(str2, length)
    res = ''
    for ch1, ch2 in zip(text1, text2):
        ch = (ord(ch1) - ord('0')) + (ord(ch2) - ord('0')) + ord('0')
        if ch > ord('9'):
            ch = ch - ord('9') + ord('A')
        res += chr(ch)
    return text1, text2, res


def generate_one_pair1(left1, left2):
    left1 = str(left1)
    left2 = str(left2)
    text1, text2, target = sum_two_str(left1, left2)
    text = f"{text1}+{text2}="
    return text, target


def data_gen_number(batch_size, nbatches):
    "Generate random data for a src-tgt copy task."
    for i in range(nbatches):
        batch = []
        tgt_batch = []
        for j in range(batch_size):
            left1 = random.randint(0, 1000)
            left2 = random.randint(0, 1000)
            text, target = generate_one_pair1(left1, left2)
            batch.append(to_tokens(text))
            tgt_batch.append(to_tokens(target))

        data = padding_batch(batch)
        data = torch.tensor(data)
        tgt_data = padding_batch(tgt_batch)
        tgt_data = torch.tensor(tgt_data)
        src = data.requires_grad_(False).clone().detach()
        tgt = tgt_data.requires_grad_(False).clone().detach()
        yield Batch(src, tgt, pad_symbol)


def save_model(model, path):
    torch.save(model.state_dict(), path)


def load_model(model, path):
    if os.path.isfile(path):
        model.load_state_dict(torch.load(path))
    return model


def calculate(folder="./models"):
    V = voca_size
    model = make_model(V, V, N=2)
    os.makedirs(folder, exist_ok=True)
    load_model(model, os.path.join(folder, "best.pt"))
    model.eval()
    src = generate_input_batch("100+101=")
    max_len = src.shape[1] + 10
    src_mask = torch.ones(1, 1, src.shape[1])
    res = greedy_decode(model, src, src_mask, max_len=max_len, start_symbol=start_symbol)
    print(from_tokens(res[0]))


def generate_dataset(folder="./dataset"):
    file1 = os.path.join(folder, "plus.txt")
    lines = []
    for i in range(0, 1000):
        for j in range(0, 1000):
            text, target = generate_one_pair1(i, j)
            lines.append(f"{text}{target}")
    with open(file1, "w") as f:
        f.writelines([line + "\n" for line in lines])


def items_range_generate(start, end, count=None):
    x_items = []
    if count is not None:
        for _ in range(0, count):
            x_items.append(random.randrange(start, end))
    else:
        x_items = range(start, end)
    return x_items


def dataset_range(start, end, batch_size, sample=0.0, count=None, x_items=None, y_items=None):
    src_batch = []
    tgt_batch = []
    if x_items is None:
        x_items = items_range_generate(start, end, count)
    if y_items is None:
        y_items = items_range_generate(start, end, count)

    for i in x_items:
        for j in y_items:
            if random.random() < sample:
                continue
            text, target = generate_one_pair1(i, j)
            src_batch.append(to_tokens(text))
            tgt_batch.append(to_tokens(target))
            if len(src_batch) == batch_size:
                data = padding_batch(src_batch)
                data = torch.tensor(data)
                tgt_data = padding_batch(tgt_batch)
                tgt_data = torch.tensor(tgt_data)
                src = data.requires_grad_(False).clone().detach()
                tgt = tgt_data.requires_grad_(False).clone().detach()
                yield Batch(src, tgt, 0)
                src_batch = []
                tgt_batch = []


def evaluate_dataset(model, start, end, output_files):
    model.eval()
    lines = []
    for i in range(start, end):
        for j in range(start, end):
            text, target = generate_one_pair(i, j, "+")
            src = generate_input_batch(text)
            max_len = src.shape[1] + 10
            src_mask = torch.ones(1, 1, src.shape[1])
            res = greedy_decode(model, src.to(device=device), src_mask.to(device=device), max_len=max_len,
                                start_symbol=0)
            res_text = from_tokens(res[0])
            lines.append(f"{text}{target}")
            lines.append(f"{res_text}")
    with open(output_files, 'w') as f:
        f.writelines([line + "\n" for line in lines])


def evaluate(x, y, folder="./models4"):
    V = voca_size
    model = make_model(V, V, N=2)
    model.to(device=device)

    os.makedirs(folder, exist_ok=True)
    load_model(model, os.path.join(folder, "999.pt"))
    model.eval()
    text, target = generate_one_pair1(x, y)
    src = generate_input_batch(text, "$$")
    tgt = generate_input_batch(target)
    max_len = src.shape[1] + 10
    src = src.to(device=device)
    src_mask = src != pad_symbol
    res = greedy_decode(model, src, src_mask.to(device=device), max_len=max_len, start_symbol=start_symbol)
    print(text + ":" + target + "=" + from_tokens(res[0]))


def train_calculator_model(folder="./models4"):
    V = voca_size
    criterion = LabelSmoothing(size=V, padding_idx=0, smoothing=0.0)
    model = make_model(V, V, N=2)
    model.to(device=device)

    os.makedirs(folder, exist_ok=True)
    load_model(model, os.path.join(folder, "999.pt"))

    optimizer = torch.optim.Adam(
        model.parameters(), lr=0.5, betas=(0.9, 0.98), eps=1e-9
    )
    lr_scheduler = LambdaLR(
        optimizer=optimizer,
        lr_lambda=lambda step: rate(
            step, model_size=model.src_embed[0].d_model, factor=1.0, warmup=400
        ),
    )

    batch_size = 80
    best_loss = 1000000
    best_path = None
    for epoch in range(359, 1000):
        print(f"Epoch {epoch}")
        model.train()
        run_epoch(
            dataset_range(1, 100, batch_size),
            model,
            SimpleLossCompute(model.generator, criterion),
            optimizer,
            lr_scheduler,
            mode="train",
        )
        model.eval()
        print(f"Start evaluation {epoch}")
        loss = run_epoch(
            dataset_range(1, 100, batch_size, 0.8),
            model,
            SimpleLossCompute(model.generator, criterion),
            DummyOptimizer(),
            DummyScheduler(),
            mode="eval",
        )[0]
        print(f"Epoch {epoch}'s evaluation loss ${loss}")
        model_path = os.path.join(folder, f"{epoch}.pt")
        save_model(model, model_path)
        if loss < best_loss:
            best_loss = loss
            best_path = model_path
        # filepath = os.path.join("res", f"{epoch}.txt")
        # print(f"Epoch {epoch} start write evaluation result to {filepath}")
        # evaluate_dataset(model, 90, 100, filepath)

    load_model(model, best_path)
    shutil.copyfile(best_path, os.path.join(folder, 'best.pt'))
    model.eval()
    src = generate_input_batch("10000+1001").to(device=device)
    max_len = src.shape[1] + 10
    src_mask = torch.ones(1, 1, src.shape[1]).to(device=device)
    res = greedy_decode(model, src, src_mask, max_len=max_len, start_symbol=0)
    print(res)
    print(from_tokens(res[0]))


def meta_learn_train_calculator_model(folder="./models4"):
    V = voca_size
    criterion = LabelSmoothing(size=V, padding_idx=0, smoothing=0.0)
    model = make_model(V, V, N=2)
    model.to(device=device)

    os.makedirs(folder, exist_ok=True)
    load_model(model, os.path.join(folder, "999.pt"))

    optimizer = torch.optim.Adam(
        model.parameters(), lr=0.5, betas=(0.9, 0.98), eps=1e-9
    )
    lr_scheduler = LambdaLR(
        optimizer=optimizer,
        lr_lambda=lambda step: rate(
            step, model_size=model.src_embed[0].d_model, factor=1.0, warmup=400
        ),
    )

    batch_size = 80
    best_loss = 1000000
    best_path = None
    for epoch in range(1, 10):
        support_x_range = items_range_generate(0, 10 ** epoch, count=200)
        support_y_range = items_range_generate(0, 10 ** epoch, count=200)
        print(f"Start epoch {epoch}")
        for support_epoch in range(1, 100):
            print(f"Supported Epoch {epoch}/{support_epoch}")
            model.train()
            run_epoch(
                dataset_range(0, 10 ** epoch, batch_size, x_items=support_x_range, y_items=support_y_range),
                model,
                SimpleLossCompute(model.generator, criterion),
                optimizer,
                lr_scheduler,
                mode="train",
            )
            model.eval()
            print(f"Start support epoch evaluation {epoch}/{support_epoch}")
            loss = run_epoch(
                dataset_range(1, 100, batch_size, 0.8, x_items=support_x_range, y_items=support_y_range),
                model,
                SimpleLossCompute(model.generator, criterion),
                DummyOptimizer(),
                DummyScheduler(),
                mode="eval",
            )[0]
            print(f"support epoch {epoch}/{support_epoch}'s evaluation loss ${loss}")
            model_path = os.path.join(folder, f"{epoch}-{support_epoch}-support.pt")
            save_model(model, model_path)
            if loss < best_loss:
                best_loss = loss
                best_path = model_path
        query_x_range = items_range_generate(0, 10 ** epoch, count=200)
        query_y_range = items_range_generate(0, 10 ** epoch, count=200)
        for query_epoch in range(1, 100):
            print(f"Query Epoch {epoch}/{query_epoch}")
            model.train()
            run_epoch(
                dataset_range(10 ** (epoch - 1), 10 ** epoch, batch_size, count=50, x_items=query_x_range, y_items=query_y_range),
                model,
                SimpleLossCompute(model.generator, criterion),
                optimizer,
                lr_scheduler,
                mode="train",
            )
            model.eval()
            print(f"Start Query Epoch evaluation {epoch}/{query_epoch}")
            loss = run_epoch(
                dataset_range(10 ** (epoch - 1), 10 ** epoch, batch_size, 0.2, x_items=query_x_range, y_items=query_y_range),
                model,
                SimpleLossCompute(model.generator, criterion),
                DummyOptimizer(),
                DummyScheduler(),
                mode="eval",
            )[0]
            print(f"Query Epoch {epoch}/{query_epoch}'s evaluation loss ${loss}")
            model_path = os.path.join(folder, f"{epoch}-{query_epoch}-query.pt")
            save_model(model, model_path)
            if loss < best_loss:
                best_loss = loss
                best_path = model_path

    load_model(model, best_path)
    shutil.copyfile(best_path, os.path.join(folder, 'best.pt'))
    model.eval()
    src = generate_input_batch("10000+1001").to(device=device)
    max_len = src.shape[1] + 10
    src_mask = torch.ones(1, 1, src.shape[1]).to(device=device)
    res = greedy_decode(model, src, src_mask, max_len=max_len, start_symbol=0)
    print(res)
    print(from_tokens(res[0]))


if __name__ == "__main__":
    # print(sum_two_str('123456789','1234567890'))
    # generate_dataset()
    meta_learn_train_calculator_model("models4")

    start = 1000
    end = 1010
    for i in range(start, end):
        for j in range(start, end):
            evaluate(i, j)
