import os
import certifi
from pymongo import MongoClient
from pymongo.errors import ConfigurationError


def _create_client():
    uri = os.getenv("DB_URL", "mongodb://localhost:27017/")
    return MongoClient(uri, tlsCAFile=certifi.where())


def _get_database():
    client = _create_client()
    try:
        db = client.get_default_database()
    except ConfigurationError:
        db = None

    if db is None:
        db_name = os.getenv("DB_NAME", "hearhelper")
        db = client[db_name]
    return db


_DB = _get_database()
USERS_COLLECTION = _DB["users"]
