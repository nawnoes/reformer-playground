import warnings
warnings.filterwarnings("ignore")

from transformers import BertTokenizer
import torch
from model.autoregressive import ReformerAutoRegressiveModel
from util.generate import top_k

def sentence_mask_to_max_length(token_indices, max_length, pad_token_id = 0):
    token_len = len(token_indices)
    remainder = token_len % max_length
    diff_len = max_length - remainder
    result = token_indices + [pad_token_id]*diff_len
    return result

if __name__ =="__main__":
    wordpiece_vocab_path = "../data/wpm-vocab-all.txt"
    PATH= "../checkpoints/1m_step_autoregressive_model_state_dict.pt"

    # Model Hyperparameter
    """
    Model Name     layer      d_model      n_head    d_head    batchsize     learning rate     n_params
    GPT-3 samll     12         768           12        64         0.5M        6.0 x 10^-4       125M
    GPT-3 medium    24         1024          16        65         0.5M        3.0 x 10^-4       350M
    """
    max_len = 5120 # AxialPositionalEmbedding을 위한 (79,64) 값 and max_len/(bucket_size*2) ==0이어야함.
    batch_size = 2
    dim = 768
    depth = 12
    heads = 12
    causal = True # True for Auto Regressive,

    # Train Hyperparameter
    tokenizer = BertTokenizer(vocab_file=wordpiece_vocab_path, do_lower_case=False)

    model = ReformerAutoRegressiveModel(
        num_tokens=tokenizer.vocab_size,
        dim=dim,
        depth=depth,
        heads=heads,
        max_seq_len=max_len,
    )
    model.load_state_dict(torch.load(PATH,map_location=torch.device('cpu')))

    sent = '단순함을 얻기란 복잡함을 얻기보다 어렵습니다. 무언가를 단순하게 만들기 위해서는 생각을 깔끔히 정리해야 합니다. '
    padd_token_id = tokenizer.pad_token_id
    tokenized_sentence = tokenizer.encode(sent,add_special_tokens=False)
    while 1:
      input_ids = sentence_mask_to_max_length([tokenizer.cls_token_id,]  + tokenized_sentence,128,0)
      input_ids = torch.tensor(input_ids).unsqueeze(0)

      output = model(input_ids)
      pred = output[0]
      next_token_pred = pred.squeeze()[len(tokenized_sentence)]
      top_k_sample = top_k(next_token_pred,9)
      gen = tokenizer.decode(top_k_sample).replace(' ','')
      if gen == '[SEP]':
          pass

      if '##'in gen:
        sent += gen.replace('##','')
      else:
        sent += ' '+gen
      print(sent)
      tokenized_sentence = tokenizer.encode(sent, add_special_tokens=False)
