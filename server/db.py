import os
from dotenv import load_dotenv
from datetime import datetime
try:
    import pymongo
except Exception as e:
    pymongo = None
    print("pymongo not available:", e)

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")

client = None
db = None
users_collection = None
searches_collection = None

if pymongo and MONGO_URI:
    try:
        client = pymongo.MongoClient(MONGO_URI)
        db = client["influencer_db"]
        users_collection = db["users"]
        searches_collection = db["searches"]
        # create TTL index for cached searches (default 24h). adjust expireAfterSeconds as needed.
        try:
            searches_collection.create_index("created_at", expireAfterSeconds=60 * 60 * 24)
        except Exception as e:
            print("could not create TTL index on searches:", e)
        print("mongodb connected (db ready)")
    except Exception as e:
        print("mongodb connection error:", e)
else:
    print("mongodb not configured; caching disabled")