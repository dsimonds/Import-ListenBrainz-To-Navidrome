import argparse
import datetime
import json
import os
import glob
import sqlite3
import time
from rapidfuzz import fuzz, process

#region Global Configs
# User Configuration
listenbrainz_export_path = './listenbrainz_1Maple_1786052646' #'/path/to/listenbrainz/json/files'  # Replace with the actual path to your JSON files
navidrome_db_path = 'navidrome.db'  # Replace with the actual path to your Navidrome database file
username = 'dsimonds'  # Replace with your actual username

# global
conn = None
parser = argparse.ArgumentParser(
    prog='Import ListenBrainz to Navidrome',
    description='Import ListenBrainz play count into Navidrome.db from ListenBrainz export',
    epilog='')

#endreagion

def exit_with_error(message):
    print(message)
    if conn:
        database_close(conn)
    exit(1)

#region Database Update Queries
def db_query_get_ids(recording_mbid, user_id):
    updated_rows = 0
    cursor = conn.cursor()
    cursor.execute("""
SELECT
    mf.artist_id,
    mf.album_id,
    mf.id
FROM media_file AS mf
JOIN annotation AS a ON mf.artist_id = a.item_id
WHERE user_id = ? AND
    mf.mbz_recording_id = ?;
    """, (user_id, recording_mbid))

    rows = cursor.fetchall()

    for row in rows:
        artist_id, album_id, song_id = row
        updated_rows = db_query_update_all_play_count(user_id, artist_id, album_id, song_id)
        
    return updated_rows

def db_query_update_all_play_count(user_id, artist_id, album_id, song_id):
    updated_rows = 0
    query = """
UPDATE annotation 
SET play_count = play_count + 1
WHERE
    user_id = ?
    AND item_id in (?, ?, ?);
        """
    
    with conn:
        updated_rows = conn.execute(query, (user_id, artist_id, album_id, song_id))

    return updated_rows.rowcount
    
def db_query_update_play_count(recording_mbid, song, artist, user_id):
    updated_rows = 0
    query = """
UPDATE annotation 
SET play_count = play_count + 1
WHERE 
    user_id = ? AND
    item_id IN
    (SELECT mf.id
    FROM media_file mf
    WHERE 
        mf.mbz_recording_id = 
            ? AND 
            mf.artist like ? AND 
            mf.title like ?);
    """

    with conn:
            updated_rows = conn.execute(query, (user_id, recording_mbid, artist, song))

    # if updated_rows.rowcount > 0:
    #     print(f"Updated play count for {artist} - {song}. New play count: {updated_rows.rowcount}")    
    
    return updated_rows.rowcount  # Return the number of rows updated

def db_query_update_play_count_fuzzy(name, artist, user_id):
    cursor = conn.cursor()
    cursor.execute("""
SELECT
    mf.artist,
    mf.title,
    mf.mbz_recording_id
FROM media_file AS mf
JOIN annotation AS a ON mf.artist_id = a.item_id
WHERE user_id = ? AND
    mf.artist like ? AND 
    mf.title like ?;
    """, (user_id, artist, name))

    rows = cursor.fetchall()

    for row in rows:
        artist, title, recording_mbid = row
        similarity_artist = fuzz.ratio(artist.lower(), artist.lower())
        similarity_title = fuzz.ratio(title.lower(), name.lower())

        if similarity_artist > 80 and similarity_title > 80:
            # print(f"Fuzzy match found: {artist} - {title} (Play Count: {play_count})")
            return db_query_update_play_count(recording_mbid, title, artist, user_id)

    return 0

def db_query_update_album_play_count(recording_mbid, user_id):
    query = """
UPDATE annotation
SET play_count = play_count + 1
WHERE user_id = ?
    AND item_id = (
        SELECT mf.album_id
          FROM media_file AS mf
          WHERE mf.mbz_recording_id like ?
    );
    """

    with conn:
        updated_rows = conn.execute(query, (user_id, recording_mbid))

    return updated_rows.rowcount  # Return the number of rows updated

def db_query_update_artist_play_count(recording_mbid, user_id):
    query = """
UPDATE annotation
SET play_count = play_count + 1
WHERE user_id = ?
    AND item_id = (
        SELECT mf.artist_id
          FROM media_file AS mf
          WHERE mf.mbz_recording_id like ?
    );
    """

    with conn:
        updated_rows = conn.execute(query, (user_id, recording_mbid))

    return updated_rows.rowcount  # Return the number of rows updated

def db_query_update_artist_and_ablum_play_count(recording_mbid, user_id):
    query = """
UPDATE annotation
SET play_count = play_count + 1
WHERE user_id = ?
  AND (
    item_id IN (
      SELECT mf.artist_id
        FROM media_file AS mf
        WHERE mf.mbz_recording_id like ?
    )
    OR item_ID IN (
      SELECT mf.album_id
        FROM media_file AS mf
        WHERE mf.mbz_recording_id like ?
    )
  );
    """

    with conn:
        updated_rows = conn.execute(query, (user_id, recording_mbid, recording_mbid))
    
    return updated_rows.rowcount  # Return the number of rows updated

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
        mf.title like ?);
    """
    
    with conn:
        cursor = conn.cursor()
        cursor.execute(query, (user_id, recording_mbid, artist, song))
        rows = cursor.fetchall()
        # print(f"rows: {rows}. rows[0][0]: {rows[0][0] if rows else 'No rows found'}")
        if rows:
            return rows[0][0]  # Return the play_count

        # print(f"{artist}: {song}. {rows[0][0] if rows else 'No rows found'}")

def db_query_clear_play_count(user_id):

    if not args.reset_count_all:
        return
    
    query = """
UPDATE annotation
SET play_count = 0
WHERE user_id = ?;
    """

    with conn:
        conn.execute(query, (user_id,))
#endregion

#region Database Connection
def db_get_userid(username):
    with conn:
        cursor = conn.execute("SELECT id FROM user WHERE user_name = ?;", (username,))
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

#endregion

def process_json_file(file):
    start_time = time.perf_counter()

    lineNum = 0
    updated_rows = 0
    total_song_play_count = 0
    total_fuzzy_attempts = 0
    total_album_play_count = 0
    total_artist_play_count = 0
    with open(file, encoding='utf-8', mode='r') as currentFile:
        lines = currentFile.readlines()
        remaining_lines = []
        for line in lines:
            
            lineNum += 1
            if line.strip():  # Skip empty lines
                data = json.loads(line.strip())
                # print(f"{data}")
                # print (f"Data: {data}")
                name, artist, recording_mbid = None, None, None
                try:
                    name = data.get("track_metadata", {}).get("track_name", "Unknown")
                    artist = data.get("track_metadata", {}).get("artist_name", "Unknown")
                    # print(f"Processing: {artist} - {name}")
                    # recording_mbid = data.get("track_metadata", {}).get("mbid_mapping", {}).get("recording_mbid", "")
                    try:
                        recording_mbid = data.get("track_metadata", {}).get("mbid_mapping", {}).get("recording_mbid", "Unknown")
                        updated_rows = db_query_update_play_count(recording_mbid, name, artist, user_id)
                    except Exception as e:
                        # print(f"{data}")
                        # print(f"Error extracting recording_mbid from line {lineNum} for {artist} - {name} in {file}")
                        # print(f"  Exception: {e}")
                        pass  # Continue processing even if there's an error extracting recording_mbid

                    # rows = db_query_get_play_count(recording_mbid, name, artist, user_id)
                    # print(f"updated_rows: {updated_rows}.")


                    if updated_rows is None or updated_rows == 0:
                        # print(f"No exact match found for {artist} - {name}. Attempting fuzzy matching...")
                        # updated_rows = db_query_update_play_count_fuzzy(name, artist, user_id)
                        # total_fuzzy_attempts += updated_rows
                        pass
                    else:
                        # print(f"Updated play count. New play count: {updated_rows}")
                        remaining_lines.append(line)
                        total_song_play_count += updated_rows

                    # if updated_rows > 0:
                    #     # db_query_update_artist_and_ablum_play_count(recording_mbid, user_id)
                    #     total_album_play_count += db_query_update_album_play_count(recording_mbid, user_id)
                    #     total_artist_play_count += db_query_update_artist_play_count(recording_mbid, user_id)

                except Exception as e:
                    print(f"Error processing line {lineNum} in {file}")
                    # print(f"  Data: {data}")
                    print(f"  Exception: {e}")

    if args.remove_completed_songs:
        with open(file, 'w', encoding='utf-8') as currentFile:
            currentFile.writelines(remaining_lines)

    end_time = time.perf_counter()
    print(f"Processed {lineNum} lines in {(end_time - start_time):.2f}s")

    # print(f"total_fuzzy_attempts: {total_fuzzy_attempts}")
    return lineNum, total_song_play_count, total_fuzzy_attempts, total_album_play_count, total_artist_play_count

def main(path):
    if path == "./":
        path = os.getcwd()
    # files = glob.glob(os.path.join(path, '**/*.jsonl'), recursive=True)

    files = []
    if os.path.isfile(path):
        files = glob.glob(path)
        print(f"Processing file: {path}")
    elif os.path.isdir(path):
        files = glob.glob(os.path.join(path, '**/*.jsonl'), recursive=True)
        print(f"dir: {path}")
    else:
        print(f"invalid file path")

    print(f"Found {len(files)} JSON files in the directory: {path}")

    fileCount = 0
    lineCount = 0
    total_song_play_count = 0
    song_play_count, fuzzy_attempts, album_play_count, artist_play_count = 0, 0, 0, 0
    for file in files:
        print (f"Processing file: {file}")
        fileCount += 1
        lines, song_play_count, fuzzy_attempts, album_play_count, artist_play_count = process_json_file(file)
        lineCount += lines
        total_song_play_count += song_play_count


    print(f"Processed {fileCount} JSON files")
    print(f"Processed {lineCount} songs plays in total")
    print(f"Total song play count updated: {total_song_play_count}")
    # print(f"  Fuzzy: {fuzzy_attempts}. Album: {album_play_count} Artist: {artist_play_count}")


#region Start
parser.add_argument('--reset-count-all', action='store_true', default=False, help='Reset play count for entire library')
parser.add_argument('--reset-count-per-song', action='store_true', default=False, help='Only reset play count if that song is updated')
parser.add_argument('-p', '--path', action='store', default='./', help='File path to ListenBrainz Export. Defaults to current directory')
parser.add_argument('-r', '--remove-completed-songs', action='store_true', default=False, help='Remove lines from JSON files when song is processed')
args = parser.parse_args()

total_start_time = time.perf_counter()
conn = database_connect(conn)
user_id = db_get_userid(username)
if user_id is None:
    exit_with_error("User ID not found.")

db_query_clear_play_count(user_id)
main(args.path)
database_close(conn)

total_end_time = time.perf_counter()
total_time_formatted = str(datetime.timedelta(seconds=(total_end_time - total_start_time)))
print(f"Completed in {total_end_time - total_start_time}")
print(f"Completed in {total_time_formatted}")
#endregion