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
from collections import Counter, defaultdict
from rapidfuzz import fuzz
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from collections import namedtuple

#region Global Configs

# User Configuration
listenbrainz_export_path = './listenbrainz_1Maple_1786052646' #'/path/to/listenbrainz/json/files'  # Replace with the actual path to your JSON files
navidrome_db_path = 'navidrome.db'  # Replace with the actual path to your Navidrome database file
username = 'username'  # Replace with your actual username

parser = argparse.ArgumentParser(
    prog='Import ListenBrainz to Navidrome',
    description='Import ListenBrainz play count into Navidrome.db using ListenBrainz export',
    epilog='')

#endregion

def signal_handler(sig, frame):
    print("\n[!] SIGINT (stop signal) detected. Performing clean-up tasks...")
    sys.exit(0)
    exit(0)

def exit_script():
    sys.exit(0)
    exit(0)

# Remove's superfluous text from song title. Convert interchangeable chars to wildcard
# Ex: "Song's Title - Remastered" OR "Song's Title (Deluxe Edition)" -> "Song_s Title"
def standardize_string(text):
    removed_parenthesis = False
    if text.startswith("%") and text.endswith(""):
        removed_parenthesis = True
        text = text.removeprefix("%")
        text = text.removesuffix("%")

    text = re.split(r" - | – | —", text)[0]
    text = text.replace("'", "_").replace('"', "_").replace("‘", "_").replace("’", "_").replace("“", "_").replace("-", "_").replace("—", "_").replace("–", "_")
    text = re.sub(r"[\(\[].*?[\)\]]", "", text)
    text = text.strip()

    if removed_parenthesis:
        text = "%" + text + "%"
        
    return text


#region Database Update Queries
def db_search_by_mbid(recording_mbid, release_mbid, album, listened_at, play_count, user_id):
    if not recording_mbid:
        return 0

    rows = []
    conn = database_connect()
    with conn:
        # Fallback logic: (recording_mbid + release_mbid) > (recording_mbid + album title) > (recording_mbid ONLY)
        query = """
            WITH RecordingId_And_AlbumId AS (
                SELECT artist_id, album_id, id FROM media_file WHERE mbz_recording_id = ? AND mbz_album_id = ?
            ),
            RecordingId_And_AlbumName AS (
                SELECT artist_id, album_id, id FROM media_file WHERE  mbz_recording_id = ? AND album like ?
            ),
            RecordingId_Only AS (
                SELECT artist_id, album_id, id FROM media_file WHERE  mbz_recording_id = ?
            )
            SELECT * FROM RecordingId_And_AlbumId
            UNION ALL
            SELECT * FROM RecordingId_And_AlbumName WHERE NOT EXISTS (SELECT 1 FROM RecordingId_And_AlbumId)
            UNION ALL
            SELECT * FROM RecordingId_Only WHERE NOT EXISTS (SELECT 1 FROM RecordingId_And_AlbumId) 
                AND NOT EXISTS (SELECT 1 FROM RecordingId_And_AlbumName);
        """

        cursor = conn.cursor()
        cursor.execute(query, (recording_mbid, release_mbid, recording_mbid, album, recording_mbid))
        rows = cursor.fetchall()
    
    updated_line_count = 0
    
    for row in rows:
        artist_id, album_id, song_id = row
        append_to_query_list(user_id, artist_id, album_id, song_id, listened_at, play_count)
        updated_line_count += int(play_count)

    return updated_line_count

def db_search_by_title(song, album, artist, listened_at, play_count, user_id):
    if not song:
        return 0
    
    def format_param(val, min_words=1, min_len=5):
        if not val:
            return "%"
        if len(val.split()) > min_words and len(val) > min_len:
            return f"%{val}%"
        return val

    
    song_param = format_param(song, min_words=1, min_len=5)
    album_param = format_param(album, min_words=1, min_len=0)
    artist_param = format_param(artist, min_words=1, min_len=5)

    std_song = standardize_string(song)
    song_param_std = f"%{std_song}%" if std_song != song else song_param

    std_album = standardize_string(album) if album else ""
    album_param_std = f"%{std_album}%" if (album and std_album != album) else album_param

    rows = []
    
    conn = database_connect()
    with conn:
        cursor = conn.cursor()

        # Tier 1: Search by Title + Album + Artist
        if album and artist:
            query = "SELECT artist_id, album_id, id FROM media_file WHERE title LIKE ? AND album LIKE ? AND artist LIKE ?"
            cursor.execute(query, (song_param, album_param, artist_param))
            rows = cursor.fetchall()
            
            if not rows and (song_param_std != song_param or album_param_std != album_param):
                cursor.execute(query, (song_param_std, album_param_std, artist_param))
                rows = cursor.fetchall()

        # Tier 2: Search by Title + Album
        if not rows and album:
            query = "SELECT artist_id, album_id, id FROM media_file WHERE title LIKE ? AND album LIKE ?"
            cursor.execute(query, (song_param, album_param))
            rows = cursor.fetchall()
            
            if not rows and (song_param_std != song_param or album_param_std != album_param):
                cursor.execute(query, (song_param_std, album_param_std))
                rows = cursor.fetchall()

        # Tier 3: Search by Title + Artist
        if not rows and artist:
            query = "SELECT artist_id, album_id, id FROM media_file WHERE title LIKE ? AND artist LIKE ?"
            cursor.execute(query, (song_param, artist_param))
            rows = cursor.fetchall()
            
            if not rows and (song_param_std != song_param):
                cursor.execute(query, (song_param_std, artist_param))
                rows = cursor.fetchall()

    if not rows:
        return 0

    updated_line_count = 0
    for artist_id, album_id, song_id in rows:
        with cache_lock:
            cache_dict.setdefault((song, album, artist), set()).add((song_id, album_id, artist_id))
        
        append_to_query_list(user_id, artist_id, album_id, song_id, listened_at, play_count)
        updated_line_count += int(play_count)

    return updated_line_count

def db_fuzzy_search(song, artist, listened_at, play_count, user_id):
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
            append_to_query_list(user_id, artist_id, album_id, song_id, listened_at, play_count)
            updated_row_count += 1

    return updated_row_count

def db_clear_all_play_count(user_id):
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

def append_to_query_list(user_id, artist_id, album_id, song_id, listened_at, play_count):
    listened_at_formatted = str(datetime.datetime.fromtimestamp(int(listened_at), tz=datetime.timezone.utc))
    if song_id:
        update_query_queue.put((user_id, song_id, "media_file", play_count, listened_at_formatted))
        # print(f"Put: {(user_id, song_id, "media_file", "1", listened_at_formatted)}")

    if artist_id:
        update_query_queue.put((user_id, artist_id, "artist", play_count, listened_at_formatted))
        # print(f"Put: {(user_id, artist_id, "artist", "1", listened_at_formatted)}")

    if album_id:
        update_query_queue.put((user_id, album_id, "album", play_count, listened_at_formatted))
        # print(f"Put: {(user_id, album_id, "album", "1", listened_at_formatted)}")

def process_query_queue():
    # if args.reset_count_all:
    query = """
        INSERT INTO annotation(user_id, item_id, item_type, play_count, play_date) 
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, item_id, item_type) 
            DO UPDATE SET play_count = annotation.play_count + EXCLUDED.play_count, play_date = MAX(COALESCE(play_date, 0), COALESCE(EXCLUDED.play_date, 0));
    """
    # TODO: if not args.reset_count_all, only update if play_count is larger
    # else:
        # query = """
        #     INSERT INTO annotation(user_id, item_id, item_type, play_count, play_date) 
        #     VALUES (?, ?, ?, ?, ?)
        #     ON CONFLICT(user_id, item_id, item_type) 
        #         DO UPDATE SET play_count = MAX(COALESCE(play_count, 0), COALESCE(EXCLUDED.play_count, 0)), play_date = MAX(COALESCE(play_date, 0), COALESCE(EXCLUDED.play_date, 0));
        # """

    # batch process queue
    batch_size = 500
    total_processed = 0

    print("\n")
    log(f"Total items to process (including artists, albums, and songs): {update_query_queue.qsize()}", default_log, True)
    while not update_query_queue.empty():
        batch = []
        try:
            while len(batch) < batch_size:
                batch.append(update_query_queue.get_nowait())
                
        except queue.Empty:
            pass

        if batch:
            conn = database_connect()
            with conn:
                cursor = conn.cursor()
                cursor.executemany(query, batch)
                # cursor.execute("PRAGMA wal_checkpoint(FULL);")
                # conn.execute("PRAGMA journal_mode=WAL;")
            print(f"\r\033[KRemaining queue count: {update_query_queue.qsize()}", end="", flush=True)

            total_processed += len(batch)

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
    if not os.path.exists(fname) or os.path.getsize(fname) == 0:
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
    # new_file_path = fname.rename(add_sequence_to_file(fname))
    with open(fname, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(header)
        for row, count in sorted_rows:
            writer.writerow((*row, count))

def sort_all_songs_csv(fname):
    if not os.path.exists(fname) or os.path.getsize(fname) == 0:
        return

    # 1. Read and group by the first 6 matching columns
    grouped_data = defaultdict(list)

    with open(fname, mode="r", newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        header = next(reader)

        for row in reader:
            if len(row) >= 6:
                # Group by first 6 columns (Artist, Album, etc.)
                match_key = tuple(row[:6])
                grouped_data[match_key].append(row)

    # Helper function to extract and convert the 7th column for max comparison
    def get_seventh_col_value(row):
        if len(row) < 7:
            return float("-inf")  # Fallback if the 7th column doesn't exist
        try:
            return int(row[6])  # Try integer comparison first
        except ValueError:
            try:
                return float(row[6])  # Try float if it has decimals
            except ValueError:
                return row[6]  # Fallback to string sorting if text

    # 2. Sort groups by total count (descending), then artist, album, and song title
    sorted_groups = sorted(
        grouped_data.items(),
        key=lambda x: (
            -len(x[1]),  # Primary sort: Original group count (highest first)
            x[0][0].lower() if x[0][0] else "",  # Tie-breaker 1: Artist (A-Z)
            x[0][1].lower() if x[0][1] else "",  # Tie-breaker 2: Album (A-Z)
            (
                x[0][3].lower() if len(x[0]) > 3 and x[0][3] else ""
            ),  # Tie-breaker 3: Song Title (A-Z)
        ),
    )

    # 3. Add the header column and overwrite the file safely
    if "count" not in header:
        header.append("count")

    with open(fname, mode="w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(header)

        # Write out ONLY the single best row per group
        for match_key, rows_in_group in sorted_groups:
            match_count = len(rows_in_group)  # Retain original duplication count

            # Find the individual row containing the highest value in the 7th column
            best_row = max(rows_in_group, key=get_seventh_col_value)

            # Write the single best row with the count appended
            writer.writerow(best_row + [match_count])


#end region
def write_csv_header_if_first_entry(fname):
    if os.path.exists(fname) and os.path.getsize(fname) > 0:
        return
    
    # add csv header if first entry
    header = "artist","album","song","artist_mbid","release_mbid","recording_mbid","listened_at","count"
    with file_lock_jsonl:
        with open(fname, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(header)
            
def write_songs_to_csv(data, song_metadata, fname):
    write_csv_header_if_first_entry(fname)
    
    # save report of missing songs in CSV
    with file_lock_csv:
        with open(fname, mode='a', newline='', encoding='utf-8') as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(song_metadata)

    # copy missing song json line to new jsonl file
    file = os.path.join(reporting_dir, "missing_songs.jsonl")
    with file_lock_jsonl:
        with open(file, 'a', encoding='utf-8') as jsonl_file:
            jsonl_file.write(json.dumps(data, ensure_ascii=False) + "\n")

#region main
def find_songs_in_db(data, file_play_count):
    try:
        updated_rows = 0
        songs_found = 0
        listened_at = int(data.listened_at)
        play_count = int(data.count)
        if data.recording_mbid:
            updated_rows = db_search_by_mbid(data.recording_mbid, data.release_mbid, data.album, listened_at, play_count, user_id)

        if updated_rows == 0:
            ## updated_rows = db_queries_update_by_title(data.song, data.album, data.artist, listened_at, play_count, user_id)
            updated_rows = db_search_by_title(data.song, data.album, data.artist, listened_at, play_count, user_id)

        # Fuzzy search if can't find song with MusicBrainz ID or song name
        if updated_rows == 0:
            updated_rows = db_fuzzy_search(data.song, data.artist, listened_at, play_count, user_id)
            # total_fuzzy_attempts += updated_rows

        # Save records that aren't found
        song_metadata = data.artist,data.album,data.song,data.artist_mbid,data.release_mbid,data.recording_mbid,listened_at,data.count
        if updated_rows == 0:
            try:
                write_songs_to_csv(data, song_metadata, missing_songs_csv_path)
            except Exception as e:
                print()
                log(f"Exception writing to log. {e}", default_log, True)
                log(f"{traceback.print_exc()}", default_log, True)
        elif updated_rows > 0:
            # update current file play count
            songs_found += 1
            file_play_count += updated_rows

    except Exception as e:
        print()
        log(f"{traceback.print_exc()}", default_log, True)

    return file_play_count, songs_found

# writes all songs to csv
def add_all_song_plays_to_csv(data):
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
                
        song_metadata = artist,album,song,artist_mbid,release_mbid,recording_mbid,listened_at
        write_songs_to_csv(data, song_metadata, all_songs_csv_path)
        
    except Exception as e:
        print()
        log(f"{traceback.print_exc()}", default_log, True)

def process_jsonl_file(file, total_song_play_count):
    # for diagnostics
    lineNum = 0
    file_play_count = 0

    print()

    max_workers=10
    # add all songs to csv and group play count by song
    with open(file, encoding='utf-8', mode='r') as currentFile, ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        lines = currentFile.readlines()
        for line in lines:
            lineNum += 1
            if line.strip():
                data = json.loads(line.strip())
                futures.append(executor.submit(add_all_song_plays_to_csv, data))
            print(f"\r\033[KLoading play count from file: {lineNum}", end="", flush=True)

        # for future in as_completed(futures):
        #     line_play_count, fuzzy_attempts = future.result()
        #     file_play_count += line_play_count
        #     total_fuzzy_attempts += fuzzy_attempts
        #     print(f"\r\033[KFile: {file} Play count: {file_play_count}", end="", flush=True)

        total_song_play_count += lineNum

    # print(f"total_fuzzy_attempts: {total_fuzzy_attempts}")
    return lineNum, total_song_play_count

def process_all_songs_csv(file):
    print()
    print("Searching database for song matches")

    rowNum = 0
    file_play_count = 0
    max_workers=10

    # process play
    with open(file, mode="r", newline="", encoding="utf-8") as file, ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        reader = csv.reader(file)
        header = next(reader)
        RowTuple = namedtuple("RowTuple", header, rename=True)

        for row in reader:
            rowNum += 1
            data = RowTuple(*row)
            futures.append(executor.submit(find_songs_in_db, data, file_play_count))

        song_count = 0
        print()
        for future in as_completed(futures):
            matched_song_play_count, this_song_count = future.result()
            song_count += this_song_count
            file_play_count += matched_song_play_count
            print(f"\033[A\r\033[KSong count: {song_count}", end="\n", flush=True)
            print(f"\r\033[KPlay count: {file_play_count}", end="", flush=True)

        log(f"Song count: {song_count}", default_log, False)
        log(f"Play count: {file_play_count}", default_log, False)
    return song_count, file_play_count

def main(path, all_songs_csv_path):
    if path == "./":
        path = os.getcwd()

    files = []
    if os.path.isfile(path):
        files = glob.glob(path)
    elif os.path.isdir(path):
        files = glob.glob(os.path.join(path, '**/*.jsonl'), recursive=True)
    else:
        log(f"Invalid file path", default_log, True)


    totalFileCount = len(files)
    log(f"Found {totalFileCount} JSON file(s) to process: {path}", default_log, True)
    if totalFileCount == 0:
        log(f"Nothing to process", default_log, True)
        return False

    fileCount = 0
    lineCount = 0
    total_song_play_count = 0
    this_all_songs_csv_path = all_songs_csv_path
    need_to_sort_csv = True

    # load song plays to CSV
    for file in files:
        if file.endswith('.csv'):
            if totalFileCount == 1:
                log(f"Processing results csv: {file}", default_log, True)
                this_all_songs_csv_path = file
                need_to_sort_csv = False
            continue
        total_start_time = time.perf_counter()
        fileCount += 1
        print()
        print(f"File {fileCount}/{totalFileCount} | Loading song plays from file: {file}", end="", flush=False)
        lines, total_song_play_count = process_jsonl_file(file, total_song_play_count)
        lineCount += lines
        total_end_time = time.perf_counter()
        total_time_formatted = str(datetime.timedelta(seconds=(total_end_time - total_start_time)))
        print(f"\r\033[KLoaded play count from file: {lines}", end="", flush=True)
        print()
        print(f"Completed file in {total_time_formatted}")
        log(f"Completed {lines} lines in {total_time_formatted}", default_log, False)
        log(f"Current total play count: {total_song_play_count}", default_log, True)

    # sort and group duplicate song plays in CSV if it was newly created
    if need_to_sort_csv:
        sort_all_songs_csv(this_all_songs_csv_path)


    # read CSV and update database
    song_count, file_play_count = process_all_songs_csv(this_all_songs_csv_path)

    # process queue of queries
    process_query_queue()

    print("\n")
    log("----- SUMMARY -----", default_log, True)
    log(f"Processed {fileCount} JSON file(s)", default_log, True)
    log(f"Processed {lineCount} songs in total", default_log, True)
    log(f"Updated songs: {song_count} plays: {file_play_count}", default_log, True)
    return True
#endregion

#region Start
if __name__ == '__main__':
    # argument parser
    parser.add_argument('-u', '--username', action='store', default='', help='Navidrome username')
    parser.add_argument('-p', '--path', action='store', default='./', help='File path to ListenBrainz Export. Defaults to current directory')
    parser.add_argument('-db', '--database', action='store', default='', help='Location of Navidrome.db file')
    parser.add_argument('--reset-count-all', action='store_true', default=False, help='Reset play count for entire library to start fresh')
    parser.add_argument('--reset-count-per-song', action='store_true', default=False, help='Only reset play count if that song is updated')
    parser.add_argument('-ru', '--remove-updated-songs', action='store_true', default=False, help='Remove lines from JSON files when song is processed')
    parser.add_argument('-id', '--mb_id', action='store_true', default=False, help='Improves speed by only updating if MB ID is a match, doesn''t perform text based matching')
    args = parser.parse_args()

    if args.database:
        navidrome_db_path = args.database

    if args.username:
        username = args.username
    
    signal.signal(signal.SIGINT, signal_handler)

    # create threading sync locs
    file_lock_csv = threading.Lock()
    file_lock_jsonl = threading.Lock()
    cache_lock = threading.Lock()

    # create loggers
    log_filename = "ImportListenBrainzToNavidrome.log"
    default_log = "default_log"
    setup_logger(default_log, log_filename, True, logging.DEBUG)

    now = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    reporting_dir = f"./reports/{now}"

    # setup list of all songs csv
    all_songs_filename = "all_songs.csv"
    all_songs_csv_path = Path(os.path.join(reporting_dir, all_songs_filename))
    all_songs_csv_path.parent.mkdir(parents=True, exist_ok=True)
    
    # setup missing songs csv report
    missing_songs_filename = "missing_songs.csv"
    missing_songs_csv_path = Path(os.path.join(reporting_dir, missing_songs_filename))
    missing_songs_csv_path.parent.mkdir(parents=True, exist_ok=True)

    total_start_time = time.perf_counter()

    # used for caching songs missing musicbrainz id for faster lookup
    cache_dict = {}
    cache_not_found_set = set()
    update_query_queue = queue.Queue()

    # get user id from database
    user_id = db_get_userid(username)

    if user_id is None:
        log(f"User ID not found for user: {username}. Exiting", default_log, True)
        exit_script()

    # clear all play counts if --reset-count-all argument is used
    db_clear_all_play_count(user_id)

    # begin processing
    if main(args.path, all_songs_csv_path):
        # sort csv of unmatched (not needed now that it's been sorted and grouped in the beginning)
        # sort_csv(missing_songs_csv_path)
        total_end_time = time.perf_counter()
        total_time_formatted = str(datetime.timedelta(seconds=(total_end_time - total_start_time)))
        print(f"Completed in {total_time_formatted}")
        print(f"Logs and a list of unmatched songs can be found in: {reporting_dir}")
#endregion