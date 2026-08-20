import asyncio
import os
import uuid

import chess
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

# Reads the .env file sitting next to this script and puts its values into
# os.environ. This MUST run before any os.environ.get() call below, because
# those lines execute the moment Python imports this file.
from dotenv import load_dotenv

load_dotenv()

# --- Google sign-in verification ---------------------------------------
# `uv add google-auth`
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

# The OAuth Client ID you created in Google Cloud Console.
# Put it in your .env file as: GOOGLE_CLIENT_ID=520601...apps.googleusercontent.com
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")


# ============================================================== DATABASE ==
# Firestore is Google's NoSQL database. `firebase-admin` is the SDK meant
# to run on a SERVER (not in a browser) - it has full read/write access to
# your database and bypasses all security rules, which is exactly why the
# key file must never be committed to git or sent to the frontend.
#
#   uv add firebase-admin
#
# Two ideas worth knowing before reading the code below:
#
# 1. Firestore stores "documents" (like dictionaries) inside "collections"
#    (like folders). There are no tables or schemas - each document is just
#    a bag of fields. We use two collections: "users" and "games".
#
# 2. The firebase-admin library is BLOCKING - each call waits for the
#    network. In an async app like FastAPI that would freeze the whole
#    server for every other player while one save happens. So every call
#    is wrapped in `asyncio.to_thread(...)`, which runs the blocking work
#    on a background thread and lets the event loop keep serving moves.

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

FIREBASE_KEY_PATH = os.environ.get("FIREBASE_KEY_PATH", "serviceAccountKey.json")

# On a hosting platform you can't upload a file, so the whole key JSON is
# pasted into an environment variable instead. Locally the file is used.
FIREBASE_KEY_JSON = os.environ.get("FIREBASE_KEY_JSON", "")

db = None

try:
    if FIREBASE_KEY_JSON:
        import json
        cred = credentials.Certificate(json.loads(FIREBASE_KEY_JSON))
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firestore connected (from environment variable).")
    elif os.path.exists(FIREBASE_KEY_PATH):
        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_KEY_PATH))
        db = firestore.client()
        print("Firestore connected (from key file).")
    else:
        print("No Firebase credentials found - running without a database.")
except Exception as exc:
    print("Firestore setup failed, running without a database:", exc)


def _save_user_blocking(google_id, name, email, picture):
    """
    Creates or updates one document in the "users" collection, using the
    player's Google account ID as the document ID.

    `merge=True` means "update these fields, leave any others alone" -
    so signing in a second time won't wipe out data we added elsewhere.
    Using the Google ID as the document ID also means one user can never
    accidentally get two records.
    """
    db.collection("users").document(google_id).set(
        {
            "name": name,
            "email": email,
            "picture": picture,
            "last_seen": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )


def _save_game_blocking(data):
    """`add()` creates a new document with a random auto-generated ID."""
    db.collection("games").add(data)


def _recent_games_blocking(google_id, limit):
    """
    Finds games this player took part in. We stored a `player_ids` list on
    every game precisely so we can do this in one query - "array_contains"
    matches any document whose list includes this ID, regardless of whether
    they played white or black.
    """
    query = (
        db.collection("games")
        .where(filter=FieldFilter("player_ids", "array_contains", google_id))
        .order_by("finished_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [doc.to_dict() for doc in query.stream()]


async def save_user(player):
    """Async wrapper. Failures are logged but never crash a live game."""
    if db is None or not player.google_id:
        return
    try:
        await asyncio.to_thread(
            _save_user_blocking,
            player.google_id,
            player.name,
            player.email,
            player.picture,
        )
    except Exception as exc:
        print("Could not save user:", exc)


async def save_game(room, result, reason, winner):
    """Writes one finished game to the "games" collection."""
    if db is None:
        return

    if winner is None:
        winner_name = None
    else:
        winner_name = room.white.name if winner == chess.WHITE else room.black.name

    data = {
        "white": {"id": room.white.google_id, "name": room.white.name, "email": room.white.email},
        "black": {"id": room.black.google_id, "name": room.black.name, "email": room.black.email},
        # A flat list of both IDs so we can query "games featuring player X"
        # without needing two separate queries for white and black.
        "player_ids": [p.google_id for p in room.players if p.google_id],
        "result": result,          # "1-0", "0-1" or "1/2-1/2"
        "reason": reason,          # "checkmate", "resignation", ...
        "winner": winner_name,     # None for a draw
        "moves": room.history,     # ["e4", "e5", "Nf3", ...]
        "move_count": len(room.history),
        "captured": room.captured,
        "final_fen": room.board.fen(),
        # SERVER_TIMESTAMP tells Firestore to fill in its own clock time,
        # so the value doesn't depend on your machine's clock being right.
        "finished_at": firestore.SERVER_TIMESTAMP,
    }

    try:
        await asyncio.to_thread(_save_game_blocking, data)
    except Exception as exc:
        print("Could not save game:", exc)

app = FastAPI(title="Knight Club API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_google_token(credential: str) -> dict | None:
    """
    Verifies the ID token (JWT) that the browser got from Google Sign-In.
    Returns the decoded payload (contains name/email/picture) if it's
    genuine and was issued for our GOOGLE_CLIENT_ID, otherwise None.

    Doing this check on the server (not trusting whatever the browser
    claims) is the whole point of "real" authentication - a client could
    otherwise just lie about who it is.
    """
    if not GOOGLE_CLIENT_ID:
        print("WARNING: GOOGLE_CLIENT_ID is not set - rejecting all logins.")
        return None
    try:
        info = id_token.verify_oauth2_token(
            credential, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        return info
    except Exception as exc:
        print("Google token verification failed:", exc)
        return None


# ========================================================= SERVE FRONTEND ==

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return HTMLResponse("<h1>index.html not found in current directory</h1>", status_code=404)


@app.get("/api/info")
async def info():
    return {
        "starting_fen": chess.STARTING_FEN,
        "library": f"python-chess {chess.__version__}",
        "google_client_id": GOOGLE_CLIENT_ID,
        "database": db is not None,
    }


@app.get("/api/games/{google_id}")
async def games_for_player(google_id: str, limit: int = 10):
    """
    Returns this player's most recent finished games, newest first.
    Try it in a browser once you've played a game:
        http://127.0.0.1:8000/api/games/<the long numeric google id>
    """
    if db is None:
        return {"games": [], "database": False}
    try:
        games = await asyncio.to_thread(_recent_games_blocking, google_id, limit)
        return {"games": games, "database": True}
    except Exception as exc:
        print("Could not read games:", exc)
        return {"games": [], "database": True, "error": str(exc)}


# ================================================================ PLAYERS ==

class Player:
    def __init__(
        self,
        websocket: WebSocket,
        name: str,
        email: str | None = None,
        picture: str | None = None,
        google_id: str | None = None,
    ):
        self.id = uuid.uuid4().hex[:8]
        self.ws = websocket
        self.name = name
        self.email = email
        self.picture = picture
        # Google's permanent, unique ID for this account (the "sub" claim).
        # Unlike an email address this never changes, so it's what we key
        # database records on.
        self.google_id = google_id
        self.color = None
        self.room = None

    async def send(self, msg_type: str, **data):
        try:
            await self.ws.send_json({"type": msg_type, **data})
        except Exception:
            pass


class Room:
    def __init__(self, white: Player, black: Player):
        self.id = uuid.uuid4().hex[:8]
        self.board = chess.Board()
        self.white = white
        self.black = black
        self.players = (white, black)
        self.history = []
        self.last_move = None
        self.over = False
        self.lock = asyncio.Lock()
        # Pieces each side has captured, stored as lowercase piece letters
        # ('p','n','b','r','q') - captured["white"] is the list of black
        # pieces white has taken off the board, and vice versa.
        self.captured = {"white": [], "black": []}

    def opponent_of(self, player: Player) -> Player:
        return self.black if player is self.white else self.white

    def state_for(self, player: Player) -> dict:
        my_turn = (not self.over) and self.board.turn == player.color
        return {
            "fen": self.board.fen(),
            "turn": "white" if self.board.turn == chess.WHITE else "black",
            "your_turn": my_turn,
            "legal": [m.uci() for m in self.board.legal_moves] if my_turn else [],
            "last_move": self.last_move,
            "check": self.board.is_check(),
            "history": self.history,
            "captured": self.captured,
        }

    async def broadcast_state(self):
        for player in self.players:
            await player.send("state", **self.state_for(player))

    async def finish(self, result: str, reason: str, winner):
        self.over = True
        rooms.pop(self.id, None)

        # Write the completed game to Firestore. This is the only place a
        # game ends, so it's the one spot that needs the save call.
        await save_game(self, result, reason, winner)

        for player in self.players:
            if winner is None:
                you = "draw"
            else:
                you = "win" if player.color == winner else "lose"
            await player.send("game_over", result=result, reason=reason, you=you)


# ============================================================ MATCHMAKING ==

waiting_player: Player | None = None
queue_lock = asyncio.Lock()
rooms: dict[str, Room] = {}


async def find_match(player: Player):
    global waiting_player

    async with queue_lock:
        if waiting_player is None:
            waiting_player = player
            # Send their own ID along with the wait notice, so the lobby
            # can offer "view your games" while they wait for an opponent.
            await player.send("waiting", your_id=player.google_id)
            return
        opponent = waiting_player
        waiting_player = None

    opponent.color = chess.WHITE
    player.color = chess.BLACK
    room = Room(white=opponent, black=player)
    rooms[room.id] = room
    opponent.room = room
    player.room = room

    for me in room.players:
        opponent_player = room.opponent_of(me)
        await me.send(
            "start",
            color="white" if me.color == chess.WHITE else "black",
            you=me.name,
            # The player's own Google ID, so the frontend knows which ID to
            # ask for when it loads their past games.
            your_id=me.google_id,
            opponent=opponent_player.name,
            opponent_picture=opponent_player.picture,
            **room.state_for(me),
        )


# =============================================================== PLAYING ===

async def handle_move(player: Player, uci):
    room = player.room
    if room is None or room.over:
        return

    async with room.lock:
        board = room.board

        if board.turn != player.color:
            await player.send("illegal", reason="It is not your turn.")
            return

        try:
            move = chess.Move.from_uci(str(uci))
        except ValueError:
            await player.send("illegal", reason="That is not a move.")
            return

        if move not in board.legal_moves:
            await player.send("illegal", reason="Illegal move.")
            return

        # Figure out what (if anything) this move captures. We have to
        # check this BEFORE pushing the move - once it's pushed, the
        # captured piece is gone from the board and we can't look it up
        # anymore. En passant is a special case: the captured pawn isn't
        # actually on the destination square, so board.is_capture() /
        # piece_type_at() alone would miss it.
        captured_symbol = None
        if board.is_en_passant(move):
            captured_symbol = "p"
        elif board.is_capture(move):
            captured_type = board.piece_type_at(move.to_square)
            if captured_type:
                captured_symbol = chess.piece_symbol(captured_type)

        room.history.append(board.san(move))
        board.push(move)
        room.last_move = move.uci()

        if captured_symbol:
            capturer = "white" if player.color == chess.WHITE else "black"
            room.captured[capturer].append(captured_symbol)

        outcome = board.outcome(claim_draw=True)
        if outcome is not None:
            room.over = True

        await room.broadcast_state()

        if outcome is not None:
            await room.finish(
                result=outcome.result(),
                reason=outcome.termination.name.replace("_", " ").lower(),
                winner=outcome.winner,
            )


async def handle_resign(player: Player):
    room = player.room
    if room is None or room.over:
        return
    async with room.lock:
        if room.over:
            return
        winner = not player.color
        await room.finish(
            result="0-1" if player.color == chess.WHITE else "1-0",
            reason="resignation",
            winner=winner,
        )


# ============================================================ DISCONNECTS ==

async def cleanup(player: Player | None):
    global waiting_player
    if player is None:
        return

    async with queue_lock:
        if waiting_player is player:
            waiting_player = None

    room = player.room
    if room and not room.over:
        room.over = True
        rooms.pop(room.id, None)
        await room.opponent_of(player).send("opponent_left")
    player.room = None


# ============================================================== WEBSOCKET ==

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    player: Player | None = None
    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")

            if kind == "join" and player is None:
                # The frontend no longer sends a plain-text "name" - it
                # sends the Google ID token it received from the Sign In
                # With Google button. We verify that token here, and take
                # the player's name/photo from the verified payload, so a
                # client can't just claim to be anyone it wants.
                credential = msg.get("credential")
                info = verify_google_token(credential) if credential else None

                if info is None:
                    await websocket.send_json({
                        "type": "auth_error",
                        "reason": "Google sign-in failed. Please try again.",
                    })
                    continue

                name = str(info.get("name") or info.get("email") or "Player").strip()[:20] or "Player"
                player = Player(
                    websocket,
                    name,
                    email=info.get("email"),
                    picture=info.get("picture"),
                    # "sub" is Google's term for the account's unique ID.
                    google_id=info.get("sub"),
                )
                await save_user(player)
                await find_match(player)

            elif kind == "move" and player is not None:
                await handle_move(player, msg.get("uci"))

            elif kind == "resign" and player is not None:
                await handle_resign(player)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await cleanup(player)


if __name__ == "__main__":
    import uvicorn

    # Hosting platforms assign a port and pass it in as $PORT. Locally
    # there's no such variable, so we fall back to 8000.
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)