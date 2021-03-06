"""
base on lucidrains/electra-pytorch
- url: https://github.com/lucidrains/electra-pytorch/blob/master/electra_pytorch/electra_pytorch.py
"""

import math
from functools import reduce
from collections import namedtuple

import torch
from torch import nn
import torch.nn.functional as F
from transformers.activations import get_activation

# constants

Results = namedtuple('Results', [
  'loss',
  'mlm_loss',
  'disc_loss',
  'gen_acc',
  'disc_acc',
  'disc_labels',
  'disc_predictions'
])


# helpers
# 유틸 함수
def log(t, eps=1e-9):
  return torch.log(t + eps)


def gumbel_noise(t):
  noise = torch.zeros_like(t).uniform_(0, 1)
  return -log(-log(noise))


def gumbel_sample(t, temperature=1.):
  return ((t / temperature) + gumbel_noise(t)).argmax(dim=-1)


def prob_mask_like(t, prob):
  return torch.zeros_like(t).float().uniform_(0, 1) < prob


def mask_with_tokens(t, token_ids):
  init_no_mask = torch.full_like(t, False, dtype=torch.bool)
  mask = reduce(lambda acc, el: acc | (t == el), token_ids, init_no_mask)
  return mask


def get_mask_subset_with_prob(mask, prob):
  batch, seq_len, device = *mask.shape, mask.device
  max_masked = math.ceil(prob * seq_len)

  num_tokens = mask.sum(dim=-1, keepdim=True)
  mask_excess = (mask.cumsum(dim=-1) > (num_tokens * prob).ceil())
  mask_excess = mask_excess[:, :max_masked]

  rand = torch.rand((batch, seq_len), device=device).masked_fill(~mask, -1e9)
  _, sampled_indices = rand.topk(max_masked, dim=-1)
  sampled_indices = (sampled_indices + 1).masked_fill_(mask_excess, 0)

  new_mask = torch.zeros((batch, seq_len + 1), device=device)
  new_mask.scatter_(-1, sampled_indices, 1)
  return new_mask[:, 1:].bool()


# hidden layer extractor class, for magically adding adapter to language model to be pretrained
# 히든 레이어 추출 클래스
class HiddenLayerExtractor(nn.Module):
  def __init__(self, net, layer=-2):
    super().__init__()
    self.net = net
    self.layer = layer

    self.hidden = None
    self.hook_registered = False

  def _find_layer(self):
    if type(self.layer) == str:
      modules = dict([*self.net.named_modules()])
      return modules.get(self.layer, None)
    elif type(self.layer) == int:
      children = [*self.net.children()]
      return children[self.layer]
    return None

  def _hook(self, _, __, output):
    self.hidden = output

  def _register_hook(self):
    layer = self._find_layer()
    assert layer is not None, f'hidden layer ({self.layer}) not found'
    handle = layer.register_forward_hook(self._hook)
    self.hook_registered = True

  def forward(self, x):
    if self.layer == -1:
      return self.net(x)

    if not self.hook_registered:
      self._register_hook()

    _ = self.net(x)
    hidden = self.hidden
    self.hidden = None
    assert hidden is not None, f'hidden layer {self.layer} never emitted an output'
    return hidden


# main electra class
class Electra(nn.Module):
  def __init__(
    self,
    generator,
    discriminator,
    *,
    num_tokens=None,
    discr_dim=-1,
    discr_layer=-1,
    mask_prob=0.15,
    replace_prob=0.85,
    random_token_prob=0.,
    mask_token_id=4,
    pad_token_id=0,
    mask_ignore_token_ids=[],
    disc_weight=50.,
    gen_weight=1.,
    temperature=1.):
    super().__init__()

    self.generator = generator
    self.discriminator = discriminator

    if discr_dim > 0:
      self.discriminator = nn.Sequential(
        HiddenLayerExtractor(discriminator, layer=discr_layer),
        nn.Linear(discr_dim, 1)
      )

    # mlm related probabilities
    # mlm 관련된 확률
    self.mask_prob = mask_prob
    self.replace_prob = replace_prob
    # 토큰 갯수와 램덤으로 토큰을 바꿀 확률을
    self.num_tokens = num_tokens
    self.random_token_prob = random_token_prob

    # token ids
    # 패드와 마스크 토큰 인덱스를 구하고, 계산을 무시하기 위해
    self.pad_token_id = pad_token_id
    self.mask_token_id = mask_token_id
    self.mask_ignore_token_ids = set([*mask_ignore_token_ids, pad_token_id])

    # sampling temperature
    self.temperature = temperature

    # loss weights
    self.disc_weight = disc_weight
    self.gen_weight = gen_weight

  def forward(self, input, **kwargs):
    b, t = input.shape

    replace_prob = prob_mask_like(input, self.replace_prob)

    # do not mask [pad] tokens, or any other tokens in the tokens designated to be excluded ([cls], [sep])
    # also do not include these special tokens in the tokens chosen at random
    no_mask = mask_with_tokens(input, self.mask_ignore_token_ids)
    mask = get_mask_subset_with_prob(~no_mask, self.mask_prob)

    # get mask indices
    # 마스크의 인덱스를 가져옴
    mask_indices = torch.nonzero(mask, as_tuple=True)

    # mask input with mask tokens with probability of `replace_prob` (keep tokens the same with probability 1 - replace_prob)
    masked_input = input.clone().detach()

    # if random token probability > 0 for mlm
    if self.random_token_prob > 0:
      assert self.num_tokens is not None, 'Number of tokens (num_tokens) must be passed to Electra for randomizing tokens during masked language modeling'

      random_token_prob = prob_mask_like(input, self.random_token_prob)
      random_tokens = torch.randint(0, self.num_tokens, input.shape, device=input.device)
      random_no_mask = mask_with_tokens(random_tokens, self.mask_ignore_token_ids)
      random_token_prob &= ~random_no_mask
      random_indices = torch.nonzero(random_token_prob, as_tuple=True)
      masked_input[random_indices] = random_tokens[random_indices]

    # [mask] input
    masked_input = masked_input.masked_fill(mask * replace_prob, self.mask_token_id)

    # set inverse of mask to padding tokens for labels
    gen_labels = input.masked_fill(~mask, self.pad_token_id)

    # get generator output and get mlm loss
    logits = self.generator(masked_input, **kwargs)

    # nn.CrossEntropyLoss()(logits[mask_indices].view(-1,22000),gen_labels[mask_indices])
    # 위 함수로 loss를 해도 동일
    mlm_loss = F.cross_entropy(
      logits.transpose(1, 2),
      gen_labels,
      ignore_index=self.pad_token_id
    )

    # use mask from before to select logits that need sampling
    sample_logits = logits[mask_indices]

    # sample
    sampled = gumbel_sample(sample_logits, temperature=self.temperature)

    # scatter the sampled values back to the input
    disc_input = input.clone()
    disc_input[mask_indices] = sampled.detach()

    # generate discriminator labels, with replaced as True and original as False
    disc_labels = (input != disc_input).float().detach()

    # get discriminator predictions of replaced / original
    non_padded_indices = torch.nonzero(input != self.pad_token_id, as_tuple=True)

    # get discriminator output and binary cross entropy loss
    disc_logits = self.discriminator(disc_input, **kwargs)
    disc_logits = disc_logits.reshape_as(disc_labels)

    disc_loss = F.binary_cross_entropy_with_logits(
      disc_logits[non_padded_indices],
      disc_labels[non_padded_indices]
    )

    # gather metrics
    with torch.no_grad():
      gen_predictions = torch.argmax(logits, dim=-1)
      disc_predictions = torch.round((torch.sign(disc_logits) + 1.0) * 0.5)
      gen_acc = (gen_labels[mask] == gen_predictions[mask]).float().mean()
      disc_acc = 0.5 * (disc_labels[mask] == disc_predictions[mask]).float().mean() + 0.5 * (
          disc_labels[~mask] == disc_predictions[~mask]).float().mean()

    # return weighted sum of losses
    return Results(self.gen_weight * mlm_loss + self.disc_weight * disc_loss, mlm_loss, disc_loss, gen_acc, disc_acc,
                   disc_labels, disc_predictions)



class DiscriminatorMRCHead(nn.Module):
  def __init__(self, dim, num_labels,hidden_dropout_prob=0.1):
    super().__init__()
    self.dense = nn.Linear(dim, 4*dim)
    self.dropout = nn.Dropout(hidden_dropout_prob)
    self.out_proj = nn.Linear(4*dim,num_labels)

  def forward(self, x, **kwargs):
    # x = features[:, 0, :]  # take <s> token (equiv. to [CLS])
    x = self.dropout(x)
    x = self.dense(x)
    x = get_activation("gelu")(x)  # although BERT uses tanh here, it seems Electra authors used gelu here
    x = self.dropout(x)
    x = self.out_proj(x)
    return x

class DiscriminatorMRCModel(nn.Module):
    def __init__(self, discriminator, dim, num_labels=2):
        super().__init__()
        self.electra = discriminator
        self.mrc_head = DiscriminatorMRCHead(dim, num_labels)
    def forward(self,
                input_ids=None,
                start_positions=None,
                end_positions=None,
                **kwargs):
        # 1. reformer의 출력
        outputs = self.electra(input_ids,**kwargs)

        # 2. mrc를 위한
        logits = self.mrc_head(outputs)

        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = nn.CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2
            return total_loss
        else:
            return start_logits, end_logits
