import json
import os
import glob
import sqlite3

# User Configuration
listenbrainz_export_path = '/path/to/listenbrainz/json/files'  # Replace with the actual path to your JSON files
navidrome_db_path = 'navidrome.db'  # Replace with the actual path to your Navidrome database file
username = 'user_name'  # Replace with your actual username

conn = None

def exit_with_error(message):
    print(message)
    if conn:
        database_close(conn)
    exit(1)

def db_query_update_play_count(recording_mbid, song, artist, user_id):
    query ="""
UPDATE annotation 
SET play_count = play_count + 1
WHERE 
    user_id = ? AND
    item_id =
    (SELECT mf.id
    FROM media_file mf
    WHERE 
        mf.mbz_recording_id = 
            ? AND 
            mf.artist like ? AND 
            mf.title like ?)
    """

    with conn:
            conn.execute(query, (user_id, recording_mbid, artist, song))


def db_query_get_play_count(recording_mbid, song, artist, user_id):
    query ="""
SELECT a.play_count
FROM annotation a
where
    a.user_id = ? AND
    a.item_id =
        (SELECT mf.id
        FROM media_file mf
        WHERE mf.mbz_recording_id = ? AND
        mf.artist like ? AND
        mf.title like ?)
    """
    
    with conn:
        cursor = conn.cursor()
        cursor.execute(query, (user_id, recording_mbid, artist, song))
        rows = cursor.fetchall()
        print(f"{artist}: {song}. {rows[0][0] if rows else 'No rows found'}")
    

def db_query_clear_play_count(user_id):
    query = """
UPDATE annotation
SET play_count = 0
WHERE user_id = ?
    """
    
    with conn:
        conn.execute(query, (user_id,))

def db_get_userid(username):
    with conn:
        cursor = conn.execute("SELECT id FROM user WHERE user_name = ?", (username,))
        result = cursor.fetchone()
        if result:
            return result[0]
        else:
            print(f"User '{username}' not found in the database.")
            return None

def database_connect(conn):
    conn = sqlite3.connect(navidrome_db_path)
    return conn
    
def database_close(conn):
    conn.close()


def process_json_file(file):
    lineNum = 0
    with open(file, encoding='utf-8', mode='r') as currentFile:
        for line in currentFile:
            lineNum += 1
            if line.strip():  # Skip empty lines
                data = json.loads(line.strip())
                # print (f"Data: {data}")
                name, artist, recording_mbid = None, None, None
                try:
                    name = data.get("track_metadata", {}).get("track_name", "Unknown")
                    artist = data.get("track_metadata", {}).get("artist_name", "Unknown")
                    # recording_mbid = data.get("track_metadata", {}).get("mbid_mapping", {}).get("recording_mbid", "")
                    recording_mbid = data.get("track_metadata", {}).get("mbid_mapping", {}).get("recording_mbid", "Unknown")
                    db_query_update_play_count(recording_mbid, name, artist, user_id)
                    db_query_get_play_count(recording_mbid, name, artist, user_id)
                except Exception as e:
                    print(f"Error processing line {lineNum} in {file}")
                    print(f"  Data: {data}")
                    print(f"  Exception: {e}")

def main(path):
    if path == "/path/to/listenbrainz/json/files":
        path = os.getcwd()
    files = glob.glob(os.path.join(path, '*.jsonl'))
    print(f"Found {len(files)} JSON files in the directory: {path}")

    fileCount = 0
    for file in files:
        print ("file: ",file)
        fileCount += 1
        process_json_file(file)
        

    # print("Unique recording MBIDs found:")
    # pprint.pprint(keywordList)


conn = database_connect(conn)
user_id = db_get_userid(username)
if user_id is None:
    exit_with_error("User ID not found.")

db_query_clear_play_count(user_id)
main(listenbrainz_export_path)
database_close(conn)