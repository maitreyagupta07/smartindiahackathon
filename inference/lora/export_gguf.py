from unsloth import FastLanguageModel

# --- Reload the base model exactly as it was during training ---
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="unsloth/Qwen2.5-1.5B-Instruct",
    max_seq_length=1024,
    load_in_4bit=True,
)

# --- Re-attach LoRA layers, then load the trained weights from checkpoint-24 ---
model = FastLanguageModel.get_peft_model(
    model,
    r=16,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    lora_alpha=16,
    lora_dropout=0,
    bias="none",
    use_gradient_checkpointing="unsloth",
)

# Load the trained weights into the existing 'default' adapter in place, instead of
# re-wrapping with peft.PeftModel.from_pretrained (which would drop unsloth's
# monkey-patched save_pretrained_gguf method) or model.load_adapter (which would
# try to add a second adapter named 'default' and conflict with the one already there).
from peft import set_peft_model_state_dict
from safetensors.torch import load_file

state_dict = load_file("lora-output/checkpoint-24/adapter_model.safetensors")
set_peft_model_state_dict(model, state_dict)

print("Adapter reloaded from checkpoint-24. Exporting to GGUF...")

model.save_pretrained_gguf(
    "approval-note-lora",
    tokenizer,
    quantization_method="q4_k_m",
)
print("Done. GGUF adapter saved to approval-note-lora/")
