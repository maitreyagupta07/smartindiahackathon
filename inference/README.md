# Person A — Inference Layer

Ollama server serving two models on port 11434.

## Models
- Text: qwen2.5:1.5b-instruct
- Vision: moondream

## One-time machine setup
1. Install Ollama: curl -fsSL https://ollama.com/install.sh | sh
2. Pull models: ollama pull qwen2.5:1.5b-instruct && ollama pull moondream
3. Enable parallelism and expose on network (run sudo systemctl edit ollama, add):

[Service]
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_HOST=0.0.0.0:11434"

4. sudo systemctl daemon-reload && sudo systemctl restart ollama

## Verified (see master doc §2.10)
- Shape check: pass - response/done fields confirmed for both models
- Error-path check: pass - unknown model returns 404 + error JSON, no hang
- Image test: pass - moondream correctly describes real image content via base64 images field
- Isolation check: pass - server runs correctly standalone, no dependency on other services
- VRAM: ~2768MiB combined (qwen2.5:1.5b-instruct + moondream), confirmed fits on RTX 3050 4GB with ~1.3GB headroom

## Concurrency
Verified two simultaneous requests to the text model return in comparable time to
a single request alone (not serialized), after setting OLLAMA_NUM_PARALLEL=2.

## LoRA adapter
Not yet built. Planned: fine-tune qwen2.5:1.5b-instruct on ~50-100 approval-note-style
examples via Unsloth. Fallback if training doesn't complete in time: fixed system
prompt + few-shot examples baked into the agent's prompt (Person F's side), not
labeled as a LoRA adapter to judges.
