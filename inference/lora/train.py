from unsloth import FastLanguageModel
from datasets import load_dataset
from trl import SFTTrainer
from transformers import TrainingArguments

# --- Load base model in 4-bit (fits comfortably on 4GB VRAM) ---
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="unsloth/Qwen2.5-1.5B-Instruct",
    max_seq_length=1024,
    load_in_4bit=True,
)

# --- Attach LoRA adapter layers ---
model = FastLanguageModel.get_peft_model(
    model,
    r=16,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    lora_alpha=16,
    lora_dropout=0,
    bias="none",
    use_gradient_checkpointing="unsloth",
)

# --- Load your 60 training examples ---
dataset = load_dataset("json", data_files="training_data.jsonl", split="train")

def format_example(example):
    example["text"] = (
        f"### Instruction:\n{example['instruction']}\n\n"
        f"### Input:\n{example['input']}\n\n"
        f"### Response:\n{example['output']}"
    )
    return example

dataset = dataset.map(format_example)
# --- Training configuration, tuned conservatively for 4GB VRAM ---
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    dataset_text_field="text",
    max_seq_length=1024,
    args=TrainingArguments(
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        num_train_epochs=3,
        learning_rate=2e-4,
        warmup_steps=5,
        logging_steps=5,
        output_dir="lora-output",
        bf16=True,
        optim="adamw_8bit",
        save_strategy="epoch",
    ),
)

print("Starting training...")
trainer.train()
print("Training complete. Saving adapter...")

# --- Save adapter in GGUF format so Ollama can load it directly ---
model.save_pretrained_gguf(
    "approval-note-lora",
    tokenizer,
    quantization_method="q4_k_m",
)
print("Done. GGUF adapter saved to approval-note-lora/")
