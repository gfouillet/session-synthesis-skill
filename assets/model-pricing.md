# Model Pricing Reference

Prices are in USD per 1 million tokens. Last updated: 2026-05.
Always mark computed costs as estimates (~).

## Anthropic (Claude)

| Model                        | Input ($/1M) | Output ($/1M) |
|------------------------------|-------------|--------------|
| claude-opus-4.6              | 15.00       | 75.00        |
| claude-opus-4.5              | 15.00       | 75.00        |
| claude-sonnet-4.6            | 3.00        | 15.00        |
| claude-sonnet-4.5            | 3.00        | 15.00        |
| claude-haiku-4.5             | 0.80        | 4.00         |

## OpenAI (GPT)

| Model                        | Input ($/1M) | Output ($/1M) |
|------------------------------|-------------|--------------|
| gpt-5.4                      | 2.00        | 8.00         |
| gpt-5.2                      | 1.50        | 6.00         |
| gpt-5.4-mini                 | 0.15        | 0.60         |
| gpt-5-mini                   | 0.15        | 0.60         |
| gpt-4.1                      | 2.00        | 8.00         |
| gpt-5.3-codex                | 2.00        | 8.00         |
| gpt-5.2-codex                | 1.50        | 6.00         |

## Cost Formula

```
cost = (input_tokens / 1_000_000 * input_price)
     + (output_tokens / 1_000_000 * output_price)
```

## Notes

- Prices may change — verify at provider pricing pages for accuracy
- If the model is unknown or custom, do not guess pricing; report cost as unavailable
- Cached input tokens (if applicable) are typically 50–90% cheaper; not modelled here
