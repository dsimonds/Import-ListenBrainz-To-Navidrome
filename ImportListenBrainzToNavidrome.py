import argparse
import datetime
import json
import os
import glob
import sqlite3
import time
import traceback
import signal
import sys
import logging
from rapidfuzz import fuzz, process

#region Global Configs
# User Configuration
listenbrainz_export_path = './listenbrainz_1Maple_1786052646' #'/path/to/listenbrainz/json/files'  # Replace with the actual path to your JSON files
navidrome_db_path = 'navidrome.db'  # Replace with the actual path to your Navidrome database file
username = 'dsimonds'  # Replace with your actual username

parser = argparse.ArgumentParser(
    prog='Import ListenBrainz to Navidrome',
    description='Import ListenBrainz play count into Navidrome.db from ListenBrainz export',
    epilog='')

#endregion

def signal_handler(sig, frame):
    exit_with_error('')

def exit_with_error(message):
    print(message)
    if conn:
        database_close(conn)
    sys.exit(0)

#region Database Update Queries
def db_queries_update_by_mbid(recording_mbid, release_mbid, album, listened_at, user_id):
    updated_rows = 0
    rows = []

    if not recording_mbid:
        return 0

    if release_mbid:
        query = """
SELECT
    mf.artist_id,
    mf.album_id,
    mf.id
FROM media_file AS mf
JOIN annotation AS a ON mf.artist_id = a.item_id
WHERE user_id = ?
    AND mf.mbz_recording_id = ?
    AND mf.mbz_album_id = ?;
        """
        cursor = conn.cursor()
        cursor.execute(query, (user_id, recording_mbid, release_mbid))
        rows = cursor.fetchall()


    if not rows:
        query = """
SELECT
    mf.artist_id,
    mf.album_id,
    mf.id
FROM media_file AS mf
JOIN annotation AS a ON mf.artist_id = a.item_id
WHERE user_id = ?
    AND mf.mbz_recording_id = ?
    AND mf.album like ?;
            """
        cursor = conn.cursor()
        cursor.execute(query, (user_id, recording_mbid, album))
        rows = cursor.fetchall()


    if not rows:
        query = """
SELECT
    mf.artist_id,
    mf.album_id,
    mf.id
FROM media_file AS mf
JOIN annotation AS a ON mf.artist_id = a.item_id
WHERE user_id = ?
    AND mf.mbz_recording_id = ?;
                    """
        cursor = conn.cursor()
        cursor.execute(query, (user_id, recording_mbid))

        rows = cursor.fetchall()


    updated_line_count = 0
    # if not rows:
    #     print(f"Unable to find {recording_mbid}, {release_mbid}, {album}, {user_id}")

    for row in rows:
        artist_id, album_id, song_id = row
        # updated_rows = db_query_update_all_play_count(user_id, artist_id, album_id, song_id)
        db_query_update_or_insert(user_id, artist_id, album_id, song_id, listened_at)
        updated_line_count += 1
        # print(f"Updated Rows: {updated_rows}")
        
    # return updated_rows
    return updated_line_count

def db_queries_update_by_title(song, album, artist, listened_at, user_id):
    updated_rows = 0
    rows = []

    if not song:
        return 0

    print(f"Attempting to search for {song}, {album}, {artist}")

    if album and artist:
        query = """
SELECT
    artist_id,
    album_id,
    id
FROM media_file
WHERE title like ?
    AND album like ?
    AND artist like ?;
            """
        cursor = conn.cursor()
        cursor.execute(query, (song, album, artist))
        rows = cursor.fetchall()

    if not rows or (album and not artist):
        query = """
SELECT
    artist_id,
    album_id,
    id
FROM media_file
WHERE title like ?
    AND album like ?;
            """
        cursor = conn.cursor()
        cursor.execute(query, (song, album))
        rows = cursor.fetchall()

    if not rows or (artist and not album):
        query = """
SELECT
    artist_id,
    album_id,
    id
FROM media_file
WHERE title like ?
    AND artist like ?;
            """
        cursor = conn.cursor()
        cursor.execute(query, (song, artist))
        rows = cursor.fetchall()

    updated_line_count = 0
    
    for row in rows:
        artist_id, album_id, song_id = row
        # updated_rows = db_query_update_all_play_count(user_id, artist_id, album_id, song_id)
        db_query_update_or_insert(user_id, artist_id, album_id, song_id, listened_at)
        updated_line_count += 1
        # print(f"Updated Rows: {updated_rows}")
            
    # return updated_rows
    return updated_line_count

        


def db_query_update_or_insert(user_id, artist_id, album_id, song_id, listened_at):
    
    play_date = str(datetime.datetime.fromtimestamp(listened_at, tz=datetime.timezone.utc))

    query = """
INSERT INTO annotation(user_id, item_id, item_type, play_count, play_date) 
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(user_id, item_id, item_type) 
DO UPDATE SET 
    play_count = play_count + 1,
    play_date = ?;
    """
    data = []

    if song_id:
        data.append((user_id, song_id, "media_file", "1", play_date, play_date))

    if artist_id:
        data.append((user_id, artist_id, "artist", "1", play_date, play_date))

    if album_id:
        data.append((user_id, album_id, "album", "1", play_date, play_date))


    with conn:
            updated_rows = conn.executemany(query, data)
    
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
    
    return updated_rows.rowcount

def db_query_update_play_count_fuzzy(song, artist, user_id):
    cursor = conn.cursor()
    cursor.execute("""
SELECT
    mf.artist,
    mf.title,
    mf.mbz_recording_id
FROM media_file AS mf
JOIN annotation AS a ON mf.artist_id = a.item_id
WHERE user_id = ?;
    """, (user_id,)) #(user_id, artist, name))
    
    #--AND mf.artist like ? 
    #--AND mf.title like ?;
    rows = cursor.fetchall()

    for row in rows:
        db_artist, db_title, db_recording_mbid = row
        similarity_artist = fuzz.ratio(db_artist.lower(), artist.lower())
        similarity_title = fuzz.ratio(db_title.lower(), song.lower())

        if similarity_artist > 80 and similarity_title > 80:
            # print(f"Fuzzy match found: {artist} - {title} (Play Count: {play_count})")
            return db_query_update_play_count(db_recording_mbid, db_title, db_artist, user_id)

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

    return updated_rows.rowcount

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

    return updated_rows.rowcount

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
    
    return updated_rows.rowcount

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

def setup_logger(name, fname, log_time, level=logging.INFO):
    if log_time:
        formatter = logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)s - %(message)s')
    else:
        formatter = logging.Formatter('%(message)s')

    handler = logging.FileHandler(fname)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    
    logger.propagate = False
    
    return logger

#endregion
def process_json_line(line):
    return 0

def process_json_file(file, conn, total_song_play_count):
    start_time = time.perf_counter()

    lineNum = 0
    updated_rows = 0
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
                
                song, artist, recording_mbid = None, None, None
                try:
                    song = data.get("track_metadata", {}).get("track_name", "Unknown")
                    album = data.get("track_metadata", {}).get("release_name", "Unknown")
                    artist = data.get("track_metadata", {}).get("artist_name", "Unknown")
                    listened_at = data.get("listened_at", {})
                    
                    mb_mapping = data.get("track_metadata", {}).get("mbid_mapping", {})
                    if mb_mapping:
                        recording_mbid = mb_mapping.get("recording_mbid", "Unknown")
                        release_mbid = mb_mapping.get("release_mbid", "Unknown")

                    if recording_mbid:
                        updated_rows = db_queries_update_by_mbid(recording_mbid, release_mbid, album, listened_at, user_id)

                    if updated_rows is None or updated_rows == 0:
                        updated_rows = db_queries_update_by_title(song, album, artist, listened_at, user_id)

                    # Fuzzy search if can't find song with MusicBrainz ID
                    if updated_rows is None or updated_rows == 0:
                        print(f"No exact match found for {artist} - {song}. Attempting fuzzy matching...")
                        updated_rows = db_query_update_play_count_fuzzy(song, artist, user_id)
                        total_fuzzy_attempts += updated_rows
                        pass

                    # Save records that aren't found
                    if updated_rows is None or updated_rows == 0:
                        remaining_lines.append(line)
                        missing_songs_log.info(f"{artist}, {album}, {song}")
                    elif updated_rows > 0:
                        total_song_play_count += updated_rows
                        # print(f"updated rows: {updated_rows}")
                        # print(f"total play count: {total_song_play_count}")
                        # print(f"\r\033[KArtist: {artist} Album: {album} Song: {song}", end="", flush=True)
                        print(f"\r\033[KTotal play count added: {total_song_play_count}", end="", flush=True)

                except Exception as e:
                    print() # clears print(f"\r\033[K...
                    # print(f"Error processing line {lineNum} in {file}")
                    # print(f"  Data: {data}")
                    # print(f"  Exception: {e}")
                    traceback.print_exc()

        if args.remove_completed_songs:
            with open(file, 'w', encoding='utf-8') as currentFile:
                currentFile.writelines(remaining_lines)

    print() # clears print(f"\r\033[K...
    end_time = time.perf_counter()
    print(f"Processed {lineNum} lines in {(end_time - start_time):.2f}s")

    # print(f"total_fuzzy_attempts: {total_fuzzy_attempts}")
    return lineNum, total_song_play_count, total_fuzzy_attempts, total_album_play_count, total_artist_play_count

def main(path, conn):
    if path == "./":
        path = os.getcwd()

    files = []
    if os.path.isfile(path):
        files = glob.glob(path)
        print(f"Processing file: {path}")
    elif os.path.isdir(path):
        files = glob.glob(os.path.join(path, '**/*.jsonl'), recursive=True)
        print(f"dir: {path}")
    else:
        print(f"invalid file path")

    print(f"Found {len(files)} JSON file(s) in the directory: {path}")

    fileCount = 0
    lineCount = 0
    total_song_play_count = 0
    for file in files:
        print (f"Processing file: {file}")
        fileCount += 1
        lines, total_song_play_count, fuzzy_attempts, album_play_count, artist_play_count = process_json_file(file, conn, total_song_play_count)
        lineCount += lines

    print()
    print("----- SUMMARY -----")
    print(f"Processed {fileCount} JSON file(s)")
    print(f"Processed {lineCount} songs in total")
    print(f"Total song play count updated: {total_song_play_count}")
    # print(f"  Fuzzy: {fuzzy_attempts}. Album: {album_play_count} Artist: {artist_play_count}")


#region Start
parser.add_argument('--reset-count-all', action='store_true', default=False, help='Reset play count for entire library')
parser.add_argument('--reset-count-per-song', action='store_true', default=False, help='Only reset play count if that song is updated')
parser.add_argument('-p', '--path', action='store', default='./', help='File path to ListenBrainz Export. Defaults to current directory')
parser.add_argument('-r', '--remove-completed-songs', action='store_true', default=False, help='Remove lines from JSON files when song is processed')
args = parser.parse_args()
signal.signal(signal.SIGINT, signal_handler)

# create loggers
log = setup_logger("debug_logger", "ImportListenBrainzToNavidrome.log", True, logging.DEBUG)
missing_songs_log = setup_logger("info_logger", "ImportListenBrainz_missing_songs.log", False)

total_start_time = time.perf_counter()

conn = None
conn = database_connect(conn)
user_id = db_get_userid(username)
if user_id is None:
    exit_with_error("User ID not found.")

db_query_clear_play_count(user_id)
main(args.path, conn)
database_close(conn)

total_end_time = time.perf_counter()
total_time_formatted = str(datetime.timedelta(seconds=(total_end_time - total_start_time)))
print(f"Completed in {total_time_formatted}")
#endregion