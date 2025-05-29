from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import os
import sys
import lancedb
from lancedb.rerankers import AnswerdotaiRerankers
import re
import redis
import uuid
import logging
import markdown
import json
from dotenv import load_dotenv
from redis import ConnectionPool
import time
from concurrent.futures import ThreadPoolExecutor
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

load_dotenv()

# Import prompts and model executor from app.py dependencies
from prompts import (
    HYDE_SYSTEM_PROMPT,
    HYDE_V2_SYSTEM_PROMPT,
    CHAT_SYSTEM_PROMPT,
    RERANK_PROMPT
)

from model_executor import call_ai_model

# Configuration
CONFIG = {
    'SECRET_KEY': os.urandom(24),
    'REDIS_HOST': 'localhost',
    'REDIS_PORT': 6379,
    'REDIS_DB': 0,
    'REDIS_POOL_SIZE': 10,
    'LOG_FILE': 'app.log',
    'LOG_FORMAT': '%(asctime)s - %(message)s',
    'LOG_DATE_FORMAT': '%d-%b-%y %H:%M:%S',
    'CTX_CLIENT': os.environ.get("CTX_CLIENT", "openai"),
    'CTX_MODEL': os.environ.get("CTX_MODEL", "gpt-4o-mini"),
    'CHAT_CLIENT': os.environ.get("CHAT_CLIENT", "sambanova"),
    'CHAT_MODEL': os.environ.get("CHAT_MODEL", "Meta-Llama-3.1-70B-Instruct"),
    'RERANK_CLIENT': os.environ.get("RERANK_CLIENT", "sambanova"),
    'RERANK_MODEL': os.environ.get("RERANK_MODEL", "Meta-Llama-3.1-8B-Instruct"),
    'CODEBASE_PATH': os.environ.get("CODEBASE_PATH", "./"),
    'CONTEXT_LENGTH': int(os.environ.get("CONTEXT_LENGTH", "16384"))
}

print(CONFIG)

# Pydantic models for request/response
class Message(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = CONFIG['CHAT_MODEL']
    messages: List[Message]
    max_tokens: Optional[int] = 10240
    temperature: Optional[float] = 0.6
    stream: Optional[bool] = False
    user: Optional[str] = None  # OpenAI standard user identifier

class QueryRequest(BaseModel):
    query: str
    rerank: Optional[bool] = False
    user_id: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    user_id: str

class ContextItem(BaseModel):
    name: str
    description: str
    content: str

# Logging setup
def setup_logging(config):
    formatter = logging.Formatter(
        config['LOG_FORMAT'],
        datefmt=config['LOG_DATE_FORMAT']
    )
    
    file_handler = logging.FileHandler(config['LOG_FILE'])
    file_handler.setFormatter(formatter)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# Database setup
def setup_database(codebase_path):
    try:
        normalized_path = os.path.normpath(os.path.abspath(codebase_path))
        codebase_folder_name = os.path.basename(normalized_path)

        uri = "database"
        db = lancedb.connect(uri)

        method_table = db.open_table(codebase_folder_name + "_method")
        class_table = db.open_table(codebase_folder_name + "_class")

        return method_table, class_table
    except Exception as e:
        print(f"Database setup failed: {str(e)}")
        raise

# Initialize FastAPI app
app = FastAPI(title="Code RAG API", description="FastAPI server with app.py functionality")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Setup logging
logger = setup_logging(CONFIG)

# Redis connection pooling setup
try:
    redis_pool = ConnectionPool(
        host=CONFIG['REDIS_HOST'],
        port=CONFIG['REDIS_PORT'],
        db=CONFIG['REDIS_DB'],
        max_connections=CONFIG['REDIS_POOL_SIZE']
    )
    
    # Create Redis client using the connection pool
    redis_client = redis.Redis(connection_pool=redis_pool)
    
    # Test connection
    redis_client.ping()
    print("Redis connection established successfully")
except Exception as e:
    print(f"Redis connection failed: {str(e)}")
    print("Warning: Redis functionality will be limited")
    redis_client = None

# Initialize the reranker
reranker = AnswerdotaiRerankers(column="source_code")

# Global variables for database tables (will be set on startup)
method_table = None
class_table = None

# Core functions from app.py
def hyde(query):
    messages = [
        {
            "role": "system",
            "content": HYDE_SYSTEM_PROMPT
        },
        {
            "role": "user",
            "content": f"帮助分析用户问题并查询相关上下文: {query}",
        }
    ]
    response = call_ai_model(CONFIG['CTX_CLIENT'], CONFIG['CTX_MODEL'], messages, max_tokens=400)
    logger.info(f"First HYDE response: {response}")
    return response

def hyde_v2(query, temp_context, hyde_query):
    messages = [
        {
            "role": "system",
            "content": HYDE_V2_SYSTEM_PROMPT.format(temp_context=temp_context)
        },
        {
            "role": "user",
            "content": f"分析问题并完善上下文：{query}",
        }
    ]
    response = call_ai_model(CONFIG['CTX_CLIENT'], CONFIG['CTX_MODEL'], messages, max_tokens=1024)
    logger.info(f"Second HYDE response: {response}")
    return response

def chat(query, context):
    start_time = time.time()
    
    messages = [
        {
            "role": "system",
            "content": CHAT_SYSTEM_PROMPT.format(context=context)
        },
        {
            "role": "user",
            "content": query,
        }
    ]
    response = call_ai_model(CONFIG['CHAT_CLIENT'], CONFIG['CHAT_MODEL'], messages)
    chat_time = time.time() - start_time
    logger.info(f"Chat response took: {chat_time:.2f} seconds")    
    return response

def rerank_using_small_model(query, context):
    start_time = time.time()

    messages = [
        {
            "role": "system",
            "content": RERANK_PROMPT.format(context=context)
        },
        {
            "role": "user",
            "content": query,
        }
    ]
    response = call_ai_model(CONFIG['RERANK_CLIENT'], CONFIG['RERANK_MODEL'], messages)
    chat_time = time.time() - start_time
    logger.info(f"{CONFIG['RERANK_CLIENT']} {CONFIG['RERANK_MODEL']} response took: {chat_time:.2f} seconds")
    print(response)
    return response

def process_input(input_text):
    processed_text = input_text.replace('\n', ' ').replace('\t', ' ')
    processed_text = re.sub(r'\s+', ' ', processed_text)
    processed_text = processed_text.strip()
    
    return processed_text

def generate_context(query, rerank=False):
    start_time = time.time()
    
    # First HYDE call
    hyde_query = hyde(query)
    hyde_time = time.time()
    logger.info(f"First HYDE call took: {hyde_time - start_time:.2f} seconds")

    def search_class_table():
        return class_table.search(hyde_query).limit(5).to_pandas()

    # Concurrent execution of first database searches
    def search_method_table():
        return method_table.search(hyde_query).limit(5).to_pandas()

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_class_docs = executor.submit(search_class_table)
        future_method_docs = executor.submit(search_method_table)
        class_docs = future_class_docs.result()
        method_docs = future_method_docs.result()

    first_search_time = time.time()
    logger.info(f"First DB search took: {first_search_time - hyde_time:.2f} seconds")

    temp_context = '\n'.join(method_docs['code'].tolist() + class_docs['source_code'].tolist())

    # Second HYDE call
    hyde_query_v2 = hyde_v2(query, temp_context, hyde_query)
    second_hyde_time = time.time()
    logger.info(f"Second HYDE call took: {second_hyde_time - first_search_time:.2f} seconds")

    # Concurrent execution of second database searches
    def search_class_table_v2():
        return class_table.search(hyde_query_v2)
    
    def search_method_table_v2():
        return method_table.search(hyde_query_v2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_class_search = executor.submit(search_class_table_v2)
        future_method_search = executor.submit(search_method_table_v2)
        class_search = future_class_search.result()
        method_search = future_method_search.result()

    search_time = time.time()
    logger.info(f"Second DB search took: {search_time - second_hyde_time:.2f} seconds")

    # Concurrent reranking if enabled
    logger.info(f"Reranking enabled: {rerank}")
    if rerank:
        rerank_start_time = time.time()

        def rerank_class_search():
            return class_search.rerank(reranker)
        
        def rerank_method_search():
            return method_search.rerank(reranker)

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_class_search = executor.submit(rerank_class_search)
            future_method_search = executor.submit(rerank_method_search)
            class_search = future_class_search.result()
            method_search = future_method_search.result()

        rerank_time = time.time()
        logger.info(f"Reranking took: {rerank_time - rerank_start_time:.2f} seconds")
    
    # Set final time reference point
    rerank_time = time.time() if rerank else search_time

    # Fetch top documents
    class_docs = class_search.limit(5).to_list()
    method_docs = method_search.limit(5).to_list()
    final_search_time = time.time()
    logger.info(f"Final DB search took: {final_search_time - rerank_time:.2f} seconds")

    def process_classes():
        top_5_classes = class_docs[:5]
        classes_combined = "\n\n".join(
            f"File: {doc['file_path']}\nClass Info:\n{doc['source_code']} References: \n{doc['references']}  \n END OF ROW {i}"
            for i, doc in enumerate(top_5_classes)
        )
        return rerank_using_small_model(query, classes_combined)

    def process_methods():
        top_5_methods = method_docs[:5]
        methods_combined = "\n\n".join(
            f"File: {doc['file_path']}\nCode:\n{doc['code']}" for doc in top_5_methods
        )
        return rerank_using_small_model(query, methods_combined)

    # Parallel execution of reranking
    parallel_start_time = time.time()
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_classes = executor.submit(process_classes)
        future_methods = executor.submit(process_methods)
        classes_context = future_classes.result()
        methods_context = future_methods.result()
    parallel_time = time.time() - parallel_start_time
    logger.info(f"Parallel reranking took: {parallel_time:.2f} seconds")

    final_context = f"{classes_context}\n{methods_context}"

    logger.info(f"Final context: {final_context}")
    logger.info("Context generation complete.")

    total_time = time.time() - start_time
    logger.info(f"Total context generation took: {total_time:.2f} seconds")
    return (final_context, class_docs, method_docs)

# FastAPI endpoints
@app.get("/")
async def root():
    return {"message": "Code RAG API Server", "status": "running"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.get("/v1/models")
async def list_models():
    model_name = f"{CONFIG['CHAT_MODEL']}-RAG"
    return {
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": 1706013322,
                "owned_by": "local",
                "permission": [],
                "root": model_name,
                "parent": None,
                "context_length": CONFIG['CONTEXT_LENGTH'],
            }
        ]
    }

@app.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    expected_model_name = f"{CONFIG['CHAT_MODEL']}-RAG"
    if model_id != expected_model_name:
        raise HTTPException(status_code=404, detail="Model not found")
    
    return {
        "id": model_id,
        "object": "model",
        "created": 1706013322,
        "owned_by": "local",
        "permission": [],
        "root": model_id,
        "parent": None,
        "context_length": CONFIG['CONTEXT_LENGTH'],
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    # TODO : utilize redis for caching
    """OpenAI-compatible chat completions endpoint with app.py functionality."""
    try:
        # Extract the user query from messages
        query = next(
            msg.content for msg in reversed(request.messages)
            if msg.role == "user"
        )
        
        logger.info(f"Processing query: {query}")
        
        # Initialize variables for logging
        needs_context = '@codebase' in query or 'codebase' in query.lower()
        context = ""
        class_docs = []
        method_docs = []
        
        # Generate context using app.py's generate_context function
        if needs_context:
            query_clean = query.replace('@codebase', '').strip()
            context, class_docs, method_docs = generate_context(query_clean, True)
            logger.info("Generated context for query with codebase reference.")
        
        # Generate chat response using app.py's chat function
        response_text = chat(query, context[:CONFIG['CONTEXT_LENGTH']])
        
        # Format response in OpenAI format
        response_data = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_text
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": len(query.split()),
                "completion_tokens": len(response_text.split()),
                "total_tokens": len(query.split()) + len(response_text.split())
            }
        }
        
        # Log detailed information similar to main.py
        response_id = response_data["id"]
        logger.info(f"Chat Completion Response ID: {response_id}")
        
        # Create logs directory if it doesn't exist
        import os
        os.makedirs("logs", exist_ok=True)
        
        log_data = {
            "id": response_id,
            "query": query,
            "needs_context": needs_context,
            "context_method": "generate_context" if needs_context else "none",
            "context_length": len(context) if context else 0,
            "class_docs_count": len(class_docs) if class_docs else 0,
            "method_docs_count": len(method_docs) if method_docs else 0,
            "context": context[:1000] + "..." if len(context) > 1000 else context,  # Truncate for logging
            "response": response_data,
            "request_model": request.model,
            "stream_requested": request.stream,
            "timestamp": int(time.time())
        }
        
        # Save log to file
        with open(f"logs/{response_id}.json", "w") as f:
            json.dump(log_data, f, indent=2)
        
        if request.stream:
            return StreamingResponse(
                stream_chat_response(response_data),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no"}
            )
        
        return response_data
        
    except Exception as e:
        logger.error(f"Error in chat completions: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

async def stream_chat_response(response_data):
    """Stream the chat response in OpenAI format."""
    # Send the complete response as a single chunk for simplicity
    chunk = {
        "id": response_data["id"],
        "object": "chat.completion.chunk",
        "created": response_data["created"],
        "model": response_data["model"],
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": response_data["choices"][0]["message"]["content"]
                },
                "finish_reason": None
            }
        ]
    }
    
    yield f"data: {json.dumps(chunk)}\n\n"
    
    # Send final chunk
    final_chunk = {
        "id": response_data["id"],
        "object": "chat.completion.chunk",
        "created": response_data["created"],
        "model": response_data["model"],
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }
        ]
    }
    
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: QueryRequest):
    """Main chat endpoint that handles queries with optional context generation."""
    try:
        query = request.query
        rerank = request.rerank or False
        user_id = request.user_id or str(uuid.uuid4())
        
        logger.info(f"Processing query: {query}")
        
        if '@codebase' in query:
            query = query.replace('@codebase', '').strip()
            context, class_docs, method_docs = generate_context(query, rerank)
            logger.info("Generated context for query with @codebase.")
            if redis_client:
                redis_client.set(f"user:{user_id}:chat_context", context)
        else:
            context = ""
            if redis_client:
                try:
                    context_bytes = redis_client.get(f"user:{user_id}:chat_context")
                    if context_bytes is not None:
                        context = context_bytes.decode()
                except Exception as e:
                    logger.warning(f"Failed to retrieve context from Redis: {str(e)}")
                    context = ""

        # Generate chat response
        response = chat(query, context[:CONFIG['CONTEXT_LENGTH']])

        # Store the conversation history
        if redis_client:
            try:
                redis_key = f"user:{user_id}:responses"
                combined_response = {'query': query, 'response': response}
                redis_client.rpush(redis_key, json.dumps(combined_response))
            except Exception as e:
                logger.warning(f"Failed to store conversation history: {str(e)}")

        return ChatResponse(response=response, user_id=user_id)
        
    except Exception as e:
        logger.error(f"Error in chat endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/queryCodeRAG", response_model=List[ContextItem])
async def query_code_rag(request: QueryRequest):
    """Endpoint that returns context items in Continue format."""
    try:
        query = request.query
        user_id = request.user_id or str(uuid.uuid4())
        
        final_context, class_docs, method_docs = generate_context(query, True)
        logger.info("Generated context for query with CodeRAG.")
        if redis_client:
            try:
                redis_client.set(f"user:{user_id}:chat_context", final_context)
            except Exception as e:
                logger.warning(f"Failed to store context in Redis: {str(e)}")

        # Construct the "context item" format expected by Continue
        context_items = [
            ContextItem(
                name="Context Summary",
                description=f"Context retrieval summary based on user input: {query}",
                content=final_context
            )
        ]
        
        for i, doc in enumerate(class_docs):
            context_items.append(ContextItem(
                name=doc['class_name'],
                description=f"Top {i+1} Class {doc['class_name']} in file {doc['file_path']}",
                content=doc['source_code']
            ))
            
        for i, doc in enumerate(method_docs):
            context_items.append(ContextItem(
                name=f"{doc['class_name']}.{doc['name']}",
                description=f"Top{i+1} method: {doc['class_name']}.{doc['name']}\n{doc['doc_comment']}",
                content=doc['source_code']
            ))

        return context_items
        
    except Exception as e:
        logger.error(f"Error in queryCodeRAG endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/conversation/{user_id}")
async def get_conversation_history(user_id: str, limit: int = 5):
    """Get conversation history for a user."""
    try:
        if not redis_client:
            return {"user_id": user_id, "responses": [], "message": "Redis not available"}
        
        redis_key = f"user:{user_id}:responses"
        responses = redis_client.lrange(redis_key, -limit, -1)
        responses = [json.loads(resp.decode()) for resp in responses]
        return {"user_id": user_id, "responses": responses}
    except Exception as e:
        logger.error(f"Error getting conversation history: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/conversation/{user_id}")
async def clear_conversation_history(user_id: str):
    """Clear conversation history for a user."""
    try:
        if not redis_client:
            return {"message": f"Redis not available, no history to clear for user {user_id}"}
        
        redis_key = f"user:{user_id}:responses"
        context_key = f"user:{user_id}:chat_context"
        redis_client.delete(redis_key, context_key)
        return {"message": f"Conversation history cleared for user {user_id}"}
    except Exception as e:
        logger.error(f"Error clearing conversation history: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Startup event to initialize database
@app.on_event("startup")
async def startup_event():
    global method_table, class_table
    
    # Get codebase path from configuration
    codebase_path = CONFIG['CODEBASE_PATH']
    
    try:
        # Setup database
        method_table, class_table = setup_database(codebase_path)
        logger.info(f"Database initialized with codebase: {codebase_path}")
    except Exception as e:
        logger.error(f"Database initialization failed: {str(e)}")
        raise
    
    # Test Redis connection if available
    if redis_client:
        try:
            redis_client.ping()
            logger.info("Redis connection verified successfully")
        except Exception as e:
            logger.error(f"Redis connection test failed: {str(e)}")
    
    logger.info("Server starting up...")

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(app, host="0.0.0.0", port=8003)
