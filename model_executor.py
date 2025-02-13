def call_openai_model(client, model, messages, max_tokens=400):
    chat_completion = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens
    )
    return chat_completion.choices[0].message.content

# def call_ollama_model(client, model, messages, max_tokens=400):
    