import argparse
import datetime
import json
import os
import glob
import sqlite3
import time
import traceback
import re
import signal
import sys
import logging
import csv
import threading
import queue
from pathlib import Path
from collections import Counter
from rapidfuzz import fuzz
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing

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
    print("\n[!] SIGINT (stop signal) detected. Performing clean-up tasks...")
    sys.exit(0)

def exit_script():
    sys.exit(0)

def standardize_string(text):
    text = text.split(' - ', 1)[0]
    text = text.replace("'", "_").replace('"', "_").replace("‘", "_").replace("’", "_").replace("“", "_").replace("—", "_").replace("–", "_")
    text = re.sub(r"[\(\[].*?[\)\]]", "", text)
    text = text.strip()
    return text


#region Database Update Queries
def db_queries_update_by_mbid(recording_mbid, release_mbid, album, listened_at, user_id):
    if not recording_mbid:
        return 0
    
    rows = []
    
    if release_mbid:
        conn = database_connect()
        with conn:
            query = """
                SELECT artist_id, album_id, id
                FROM media_file AS mf
                WHERE mbz_recording_id = ? AND mbz_album_id = ?;
            """
            cursor = conn.cursor()
            cursor.execute(query, (recording_mbid, release_mbid))
            rows = cursor.fetchall()

    if not rows:
        conn = database_connect()
        with conn:
            query = """
                SELECT artist_id, album_id, id
                FROM media_file
                WHERE mbz_recording_id = ? AND album like ?;
            """
            cursor = conn.cursor()
            cursor.execute(query, (recording_mbid, album))
            rows = cursor.fetchall()

    if not rows:
        conn = database_connect()
        with conn:
            query = """
                SELECT artist_id, album_id, id
                FROM media_file
                WHERE mbz_recording_id = ?;
            """
            cursor = conn.cursor()
            cursor.execute(query, (recording_mbid,))
            rows = cursor.fetchall()
        
    updated_line_count = 0

    for row in rows:
        artist_id, album_id, song_id = row
        append_to_query_list(user_id, artist_id, album_id, song_id, listened_at)
        updated_line_count += 1

    return updated_line_count

def db_queries_update_by_song_id(id_list, user_id, listened_at):
    updated_row_count = 0
    
    for value in id_list:
        song_id = value[0]
        album_id = value[1]
        artist_id = value[2]
        result = db_query_update_or_insert(user_id, artist_id, album_id, song_id, listened_at)
        if result > 0:
            updated_row_count += 1
    
    return updated_row_count

def db_queries_update_by_title(song, album, artist, listened_at, user_id):
    if not song:
        return 0

    # check if song_id is already cached
    id_list = cache_dict.get((song, album, artist))
    if id_list:
        updated_row_count = db_queries_update_by_song_id(id_list, user_id, listened_at)
        return updated_row_count

    rows = []
    query_ran = None
    song_param = song
    album_param = album
    artist_param = artist
    if len(song.split()) > 1 and len(song) > 5:
        song_param = "%" + song_param + "%"
    if len(album.split()) > 1 and len(album) > 5:
        album_param = "%" + album_param + "%"
    if len(artist.split()) > 1 and len(artist) > 5:
        artist_param = "%" + artist_param + "%"
        
    if album_param and artist_param:
        conn = database_connect()
        with conn:
            query_ran = "album and artist"
            query = """
                SELECT artist_id, album_id, id
                FROM media_file
                WHERE title like ? AND album like ? AND artist like ?;
            """
            # print(f"{query}")
            cursor = conn.cursor()
            cursor.execute(query, (f"{song_param}", f"{album_param}", f"{artist_param}"))
            rows = cursor.fetchall()

    if not rows or (album_param and not artist_param):
        conn = database_connect()
        with conn:
            query_ran = "album and not artist"
            query = """
                SELECT artist_id, album_id, id
                FROM media_file
                WHERE title like ? AND album like ?;
            """
            # print(f"{query}")
            cursor = conn.cursor()
            cursor.execute(query, (f"{song_param}", f"{album_param}"))
            rows = cursor.fetchall()

    if not rows or (artist_param and not album_param):
        conn = database_connect()
        with conn:
            query_ran = "artist and not album"
            query = """
                SELECT artist_id, album_id, id
                FROM media_file
                WHERE title like ? AND artist like ?;
            """
            # print(f"{query}")
            cursor = conn.cursor()
            cursor.execute(query, (f"{song_param}", f"{artist_param}"))
            rows = cursor.fetchall()

    # remove additional text from song/album. Ex: "Song Title - Remastered" or "Song Title (Deluxe Edition)" -> "Song Title"
    if not rows:
        standardized_song = standardize_string(song)
        standardized_album = standardize_string(album)
        # if song or album is changed redo queries
        if (standardized_song != song or standardized_album != album):
            # log(f"Record not found. Retrying with standardized titles. \"{song}\" -> \"{standardized_song}\" || \"{album}\" -> \"{standardized_album}\"", default_log, True)
            return db_queries_update_by_title(standardized_song, standardized_album, artist, listened_at, user_id)

    updated_line_count = 0
    for row in rows:
        artist_id, album_id, song_id = row
        cache_dict.setdefault((song, album, artist), []).append([song_id, album_id, artist_id])
        append_to_query_list(user_id, artist_id, album_id, song_id, listened_at)
        updated_line_count += 1
            
    return updated_line_count

def db_query_update_play_count_fuzzy(song, artist, user_id, listened_at):
    updated_row_count = 0
    conn = database_connect()
    with closing(conn):
        cursor = conn.cursor()
        cursor.execute("""
            SELECT artist, title, artist_id, album_id, id
            FROM media_file
        """) #(user_id, artist, name))
        rows = cursor.fetchall()
    
    #--AND mf.artist like ? 
    #--AND mf.title like ?;

    for row in rows:
        db_artist, db_title, artist_id, album_id, song_id = row
        similarity_artist = fuzz.ratio(db_artist.lower(), artist.lower())
        similarity_title = fuzz.ratio(db_title.lower(), song.lower())

        if similarity_artist > 80 and similarity_title > 80:
            append_to_query_list(user_id, artist_id, album_id, song_id, listened_at)
            updated_row_count += 1

    return updated_row_count

def db_query_clear_play_count(user_id):

    if not args.reset_count_all:
        return

    conn = database_connect()
    with conn:
        query = """
            UPDATE annotation
            SET play_count = 0
            WHERE user_id = ?;
        """
        conn.execute(query, (user_id,))

def append_to_query_list(user_id, artist_id, album_id, song_id, listened_at):
    listened_at_formatted = str(datetime.datetime.fromtimestamp(listened_at, tz=datetime.timezone.utc))
    if song_id:
        update_query_queue.put((user_id, song_id, "media_file", 1, listened_at_formatted))
        # print(f"Put: {(user_id, song_id, "media_file", "1", listened_at_formatted, listened_at_formatted)}")

    if artist_id:
        update_query_queue.put((user_id, artist_id, "artist", 1, listened_at_formatted))
        # print(f"Put: {(user_id, artist_id, "artist", "1", listened_at_formatted, listened_at_formatted)}")

    if album_id:
        update_query_queue.put((user_id, album_id, "album", 1, listened_at_formatted))
        # print(f"Put: {(user_id, album_id, "album", "1", listened_at_formatted, listened_at_formatted)}")

    # print()
    # print(f"queue size: {update_query_queue.qsize()}")

def process_query_list():
    # update_query_queue.put((user_id, album_id, "album", 1, listened_at_formatted))
    # insert into table. if conflict (duplicate key), update record instead
    query = """
        INSERT INTO annotation(user_id, item_id, item_type, play_count, play_date) 
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, item_id, item_type) 
            DO UPDATE SET play_count = annotation.play_count + 1, play_date = EXCLUDED.play_date;
    """

    # batch process queue
    batch_size = 500
    total_processed = 0
    
    while not update_query_queue.empty():
        batch = []
        try:
            while len(batch) < batch_size:
                batch.append(update_query_queue.get_nowait())
        except queue.Empty:
            pass

        # for row in batch:
        #     conn = database_connect()
        #         with conn:
        
        if batch:
            conn = database_connect()
            with conn:
                cursor = conn.cursor()
                cursor.executemany(query, batch)
                # cursor.execute("PRAGMA wal_checkpoint(FULL);")
                # conn.execute("PRAGMA journal_mode=WAL;")

            total_processed += len(batch)

    # print(f"Processed rows: {total_processed}")

#endregion

#region Database Connection
def db_get_userid(username):
    conn = database_connect()
    with conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM user WHERE user_name = ?;", (username,))
        result = cursor.fetchone()
        cursor.close()
        if result:
            return result[0]
        else:
            log(f"User '{username}' not found in the database. Unable to continue. Exiting", default_log, True)
            return None

def database_connect():
    return sqlite3.connect(navidrome_db_path)
    
def database_close(conn):
    conn.close()

#endregion

#region logger
def setup_logger(name, fname, log_time, level=logging.INFO):
    if log_time:
        formatter = logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)s - %(message)s')
    else:
        formatter = logging.Formatter('%(message)s')

    handler = logging.FileHandler(fname, mode="a", encoding="utf-8")
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    
    logger.propagate = False
    
    return logger

def log(message, log_name="default_log", print_to_console=False):
    # print first in case write to log fails
    if print_to_console:
        print(message)
    logger = logging.getLogger(log_name)
    logger.info(message)
#endregion

#region csv processor
def add_sequence_to_file(fname):
    path = Path(fname)
    new_path = path
    counter = 1

    while new_path.exists():
        new_path = path.parent / f"{path.stem}.{counter:02d}{path.suffix}"
        counter += 1

    return new_path

def sort_csv(fname):
    if not os.path.exists(missing_songs_path) or os.path.getsize(missing_songs_path) == 0:
        return
    
    with open(fname, mode='r', newline='', encoding='utf-8') as file:
        reader = csv.reader(file)
        header = next(reader)
        row_counts = Counter(tuple(row) for row in reader)

    # sort by row count, then by artist > album > song title
    sorted_rows = sorted(
        row_counts.items(), 
        key=lambda x: (-x[1], x[0][0], x[0][1], x[0][3])
    )

    header.append("count")
    new_file_path = fname.rename(add_sequence_to_file(fname))
    with open(new_file_path, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(header)
        for row, count in sorted_rows:
            writer.writerow((*row, count))

#end region
def write_csv_header_if_first_entry():
    if os.path.exists(missing_songs_path) and os.path.getsize(missing_songs_path) > 0:
        return
    
    # add csv header if first entry
    header = "artist","album","song","artist_mbid","release_mbid","recording_mbid"
    with open(missing_songs_path, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(header)
            
def write_missing_song(data, song_metadata):
    write_csv_header_if_first_entry()
    
    # save report of missing songs in CSV
    cache_not_found_set.add(song_metadata)
    with file_lock_csv:
        with open(missing_songs_path, mode='a', newline='', encoding='utf-8') as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(song_metadata)

    # copy missing song json line to new jsonl file
    missing_songs_json = os.path.join(reporting_dir, "missing_songs.jsonl")
    with file_lock_jsonl:
        with open(missing_songs_json, 'a', encoding='utf-8') as jsonl_file:
            jsonl_file.write(json.dumps(data, ensure_ascii=False) + "\n")

#region main
def update_record(data, file_play_count, total_fuzzy_attempts):
    song = album = artist = artists = None
    recording_mbid = release_mbid = artist_mbid = None
    try:
        song = data.get("track_metadata", {}).get("track_name", "Unknown")
        album = data.get("track_metadata", {}).get("release_name", "Unknown")
        artist = data.get("track_metadata", {}).get("artist_name", "Unknown")
        listened_at = data.get("listened_at", {})
        
        mb_mapping = data.get("track_metadata", {}).get("mbid_mapping", {})
        if mb_mapping:
            recording_mbid = mb_mapping.get("recording_mbid", "Unknown")
            release_mbid = mb_mapping.get("release_mbid", "Unknown")
            artists = mb_mapping.get("artists", "Unknown")
            # if multiple artists, only get first artist in list for simplicity
            if artists:
                artist_mbid = artists[0].get("artist_mbid", "Unknown")

        song_metadata = artist,album,song,artist_mbid,release_mbid,recording_mbid
        if song_metadata in cache_not_found_set:
            write_missing_song(data, song_metadata)
            return file_play_count, total_fuzzy_attempts

        updated_rows = 0
        if recording_mbid:
            updated_rows = db_queries_update_by_mbid(recording_mbid, release_mbid, album, listened_at, user_id)

        if updated_rows == 0:
            updated_rows = db_queries_update_by_title(song, album, artist, listened_at, user_id)

        # # Fuzzy search if can't find song with MusicBrainz ID or song name
        if updated_rows == 0:
            # log(f"No exact match found for {artist} - {song}. Attempting fuzzy matching...", default_log)
            updated_rows = db_query_update_play_count_fuzzy(song, artist, user_id, listened_at)
            total_fuzzy_attempts += updated_rows

        # Save records that aren't found
        if updated_rows == 0:
            try:
                write_missing_song(data, song_metadata)
            except Exception as e:
                print()
                log(f"Exception writing to log. {e}", default_log, True)
                log(f"{traceback.print_exc()}", default_log, True)
        elif updated_rows > 0:
            file_play_count += updated_rows         # only for current file

    except Exception as e:
        print()
        log(f"{traceback.print_exc()}", default_log, True)

    return file_play_count, total_fuzzy_attempts

def process_json_file(file, total_song_play_count):
    # for diagnostics
    lineNum = 0
    total_fuzzy_attempts = 0
    total_album_play_count = 0
    total_artist_play_count = 0
    file_play_count = 0

    max_workers=10
    with open(file, encoding='utf-8', mode='r') as currentFile, ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        lines = currentFile.readlines()
        
        for line in lines:
            lineNum += 1
            if line.strip():  # Skip empty lines
                data = json.loads(line.strip())
                futures.append(executor.submit(update_record, data, file_play_count, total_fuzzy_attempts))

        for future in as_completed(futures):
            line_play_count, fuzzy_attempts = future.result()
            file_play_count += line_play_count
            total_fuzzy_attempts += fuzzy_attempts
            print(f"\r\033[KFile: {file} Play count: {file_play_count}", end="", flush=True)

        total_song_play_count += file_play_count

    process_query_list()

    # print(f"total_fuzzy_attempts: {total_fuzzy_attempts}")
    return lineNum, total_song_play_count, total_fuzzy_attempts, total_album_play_count, total_artist_play_count

def main(path):
    if path == "./":
        path = os.getcwd()

    files = []
    if os.path.isfile(path):
        files = glob.glob(path)
    elif os.path.isdir(path):
        files = glob.glob(os.path.join(path, '**/*.jsonl'), recursive=True)
    else:
        log(f"Invalid file path", default_log, True)

    log(f"Found {len(files)} JSON file(s) to process: {path}", default_log, True)

    fileCount = 0
    lineCount = 0
    total_song_play_count = 0
    for file in files:
        total_start_time = time.perf_counter()
        print()
        print(f"\r\033[KFile: {file}", end="", flush=False)
        fileCount += 1
        lines, total_song_play_count, fuzzy_attempts, album_play_count, artist_play_count = process_json_file(file, total_song_play_count)
        lineCount += lines
        total_end_time = time.perf_counter()
        total_time_formatted = str(datetime.timedelta(seconds=(total_end_time - total_start_time)))
        print()
        log(f"Completed {lines} lines in {total_time_formatted}", default_log, True)
        log(f"Current total play count: {total_song_play_count}", default_log, True)
        # log(f"Fuzzy Attempts: {fuzzy_attempts}", default_log, True)

    print()
    log("----- SUMMARY -----", default_log, True)
    log(f"Processed {fileCount} JSON file(s)", default_log, True)
    log(f"Processed {lineCount} songs in total", default_log, True)
    log(f"Total song play count updated: {total_song_play_count}", default_log, True)
#endregion

#region Start
parser.add_argument('--reset-count-all', action='store_true', default=False, help='Reset play count for entire library')
parser.add_argument('--reset-count-per-song', action='store_true', default=False, help='Only reset play count if that song is updated')
parser.add_argument('-p', '--path', action='store', default='./', help='File path to ListenBrainz Export. Defaults to current directory')
parser.add_argument('-ru', '--remove-updated-songs', action='store_true', default=False, help='Remove lines from JSON files when song is processed')
parser.add_argument('-id', '--mb_id', action='store_true', default=False, help='Improves speed by only updating if MB ID is a match, doesn''t perform text based matching')
args = parser.parse_args()
signal.signal(signal.SIGINT, signal_handler)
file_lock_csv = threading.Lock()
file_lock_jsonl = threading.Lock()

# create loggers
log_filename = "ImportListenBrainzToNavidrome.log"
default_log = "default_log"
setup_logger(default_log, log_filename, True, logging.DEBUG)

now = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
reporting_dir = f"./reports/{now}"
# setup missing songs csv report
missing_songs_filename = "missing_songs.csv"
missing_songs_path = Path(os.path.join(reporting_dir, missing_songs_filename))
missing_songs_path.parent.mkdir(parents=True, exist_ok=True)

total_start_time = time.perf_counter()

# used for songs missing musicbrainz id for faster lookup
cache_dict = {}
cache_not_found_set = set()
update_query_queue = queue.Queue()

user_id = db_get_userid(username)

if user_id is None:
    log("User ID not found. Exiting", default_log, True)
    exit_script()

db_query_clear_play_count(user_id)

main(args.path)

sort_csv(missing_songs_path)

total_end_time = time.perf_counter()
total_time_formatted = str(datetime.timedelta(seconds=(total_end_time - total_start_time)))
print(f"Completed in {total_time_formatted}")
print(f"Logs and a list of unmatched songs can be found in: {reporting_dir}")
#endregion