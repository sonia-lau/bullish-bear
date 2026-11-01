import os
from dotenv import load_dotenv

load_dotenv() 

MODEL = os.getenv('MODEL')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
MONGODB_URI = os.getenv('MONGODB_URI')
MONGODB_DB = os.getenv('MONGODB_DB')
TAVILY_API_KEY = os.getenv('TAVILY_API_KEY')
FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY')
TELEGRAM_API_KEY = os.getenv('TELEGRAM_API_KEY')