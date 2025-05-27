import os
from openai import OpenAI
import openai
from ollama import Client

# OpenAI client setup
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
sambanova_client = OpenAI(
    api_key=os.environ.get("SAMBANOVA_API_KEY"),
    base_url="https://api.sambanova.ai/v1",
)
# Local Ollama client setup
ollama_client = Client(
    host=os.environ.get("OLLAMA_SERVER", "http://localhost:11434"),
    headers={

    }
)
# Local vllm client setup
vllm_client = OpenAI(
    base_url=os.environ.get("VLLM_SERVER", "http://localhost:8000/v1"),
    api_key="fake",
    timeout=600
)

def call_openai_model(client, model, messages, max_tokens=400):
    chat_completion = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens
    )
    return chat_completion.choices[0].message.content

def call_ollama_model(model, messages, max_tokens=400):
    response = ollama_client.chat(
        model=model,
        messages=messages
    )
    return response.message.content

def call_vllm_model(model, messages, max_tokens=400):
    chat_completion = vllm_client.chat.completions.createt(
        model=model,
        messages=messages
    )
    return chat_completion.choices[0].message.content

def call_ai_model(client_type, model, messages, max_tokens=400):
    if client_type == "openai":
        return call_openai_model(openai_client, model, messages, max_tokens)
    elif client_type == "sambanova":
        return call_openai_model(sambanova_client, model, messages, max_tokens)
    elif client_type == "ollama":
        return call_ollama_model(model, messages)
    elif client_type == "vllm":
        return call_vllm_model(model, messages)
    else:
        raise ValueError("Invalid client type")

