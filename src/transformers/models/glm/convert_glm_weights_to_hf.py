
import os
import argparse
import json

import torch
from safetensors.torch import load_file as safe_load_file
from transformers import GlmConfig, GlmForCausalLM, AutoTokenizer, PreTrainedTokenizerFast
from transformers.convert_slow_tokenizer import TikTokenConverter
from tokenizers import AddedToken, Regex, Tokenizer, decoders, pre_tokenizers, processors

STATE_DICT_MAPPING = {
    "transformer.": "model.",
    "transformer.output_layer.": "lm_head.",
    ".embedding.": ".embed_tokens.",
    ".encoder.layers.": ".layers.",
    "final_layernorm.": "norm.",
    "rotary_pos_embed.": "rotary_emb.",
    "self_attention.": "self_attn.",
    "query_key_value.": "qkv_proj.",
    "dense.": "o_proj.",
    "dense_h_to_4h.": "gate_up_proj.",
    "dense_4h_to_h.": "down_proj."
}


class GlmConverter(TikTokenConverter):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def converted(self) -> Tokenizer:
        tokenizer = self.tokenizer()
        tokenizer.pre_tokenizer = pre_tokenizers.Sequence(
            [
                pre_tokenizers.Split(Regex(self.pattern), behavior="isolated", invert=False),
                pre_tokenizers.ByteLevel(add_prefix_space=self.add_prefix_space, use_regex=False),
            ]
        )
        tokenizer.decoder = decoders.ByteLevel()
        tokenizer.add_special_tokens(self.additional_special_tokens)

        tokenizer.post_processor = processors.Sequence(
            [
                processors.ByteLevel(trim_offsets=False),
                processors.TemplateProcessing(
                    single=f"[gMASK]:0 <sop>:0 $A:0",
                    pair=f"[gMASK]:0 <sop>:0 $A:0 $B:1",
                    special_tokens=[("[gMASK]", 151331), ("<sop>",151333)]
                )
            ],
        )

        return tokenizer


def merge_safetensors(input_dir: str):
    all_files = [os.path.join(input_dir, x) for x in os.listdir(input_dir) if x.endswith('.safetensors')]
    all_files = sorted(all_files, key=lambda x: int(x.split('-', 2)[1]))

    output_path = os.path.join(input_dir, 'consolidated.safetensors')
    with open(output_path, "wb") as f_out:
        for filepath in all_files:
            with open(filepath, "rb") as f_in:
                f_out.write(f_in.read())


def convert_state_dict(original_state_dict: dict):
    new_dict = {}

    for key, value in original_state_dict.items():
        new_key = key
        for old, new in STATE_DICT_MAPPING.items():
            new_key = new_key.replace(old, new)

        new_dict[new_key] = value
    return new_dict


def convert_config(original_config: dict):

    num_attention_heads = original_config.pop("num_attention_heads")

    new_config = GlmConfig(
        vocab_size=original_config.pop("padded_vocab_size"),
        hidden_size=original_config.pop("hidden_size"),
        intermediate_size=original_config.pop("ffn_hidden_size"),
        num_hidden_layers=original_config.pop("num_hidden_layer"),
        num_attention_heads=num_attention_heads,
        num_key_value_heads=num_attention_heads if not original_config.pop("multi_query_attention") else original_config.pop("multi_query_group_num"),
        resid_pdrop=original_config.pop("hidden_dropout"),
        attention_dropout=original_config.pop("attention_dropout"),
        max_position_embeddings=original_config.pop("max_position_embeddings"),
        initializer_range=original_config.pop("initializer_range"),
        rms_norm_eps=original_config.pop("layernorm_epsilon"),
        rope_theta=10000. * original_config.pop("rope_ratio"),
        use_rms_norm=original_config.pop("rmsnorm"),
        apply_residual_connection_post_layernorm=original_config.pop("apply_residual_connection_post_layernorm"),
        post_layer_norm=original_config.pop("post_layer_norm"),
        use_cache=original_config.pop("use_cache"),
        head_dim=original_config.pop("kv_channels"),
        attention_bias=original_config.pop("add_qkv_bias"),
        linear_bias=original_config.pop("add_bias_linear"),
    )
    print(f'Unused config keys: {original_config.keys(),}')
    return new_config


def convert_glm_tokenizer(input_dir):

    fast_tok = GlmConverter(os.path.join(input_dir, 'tokenizer.model'), additional_special_tokens=[]).converted()
    tokenizer = AutoTokenizer.from_pretrained("THUDM/glm-4-9b", trust_remote_code=True)
    new_tok = PreTrainedTokenizerFast(tokenizer_object=fast_tok,
                                      bos_token=tokenizer.bos_token,
                                      eos_token=tokenizer.eos_token,
                                      pad_token=tokenizer.pad_token,
                                      clean_up_tokenization_spaces=tokenizer.clean_up_tokenization_spaces,
                                      additional_special_tokens=tokenizer.additional_special_tokens,
                                      padding_side=tokenizer.padding_side
                                      )

    return new_tok


def convert_glm_model(input_dir, output_dir):
    
    # Load and convert config
    with open(os.path.join(input_dir, "config.json")) as f:
        original_config = json.load(f)
    config = convert_config(original_config)
    config.save_pretrained(output_dir)

    # Load and convert weights
    merge_safetensors(input_dir)
    original_state_dict = safe_load_file(os.path.join(input_dir, "consolidated.safetensors"))
    new_dict = convert_state_dict(original_state_dict)
    with torch.device("meta"):
        model = GlmForCausalLM.from_config(config)
    model.load_state_dict(new_dict, strict=True, assign=True)
    model.save_pretrained(output_dir)

    # Load and convert tokenizer
    tokenizer = convert_glm_tokenizer(input_dir)
    tokenizer.save_pretrained(output_dir)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "input_dir",
        type=str,
        help="Location of the local folder copied from the Hub.",
    )
    parser.add_argument(
        "output_dir",
        type=str,
        help="Location to write HF model and tokenizer",
    )

    args = parser.parse_args()
    convert_glm_model(args.input_dir, args.output_dir)
