# Knight Club

A real-time multiplayer chess app. Two players sign in with Google, get matched
automatically, and play on a shared board over a WebSocket connection.

Every move is validated on the server. The browser draws the board and reports
clicks — it does not know the rules of chess and cannot decide what is legal.

## Screenshot

<!-- Take a screenshot of a game in progress, save it as screenshot.png
     next to this file, and it will appear here. -->

![Knight Club](screenshot.png)

## What it does

- Google sign-in, with the ID token verified server-side
- Automatic matchmaking between two waiting players
- Server-authoritative move validation using `python-chess`
- Live board updates pushed to both players over a WebSocket
- Check, checkmate, stalemate, draw and resignation handling
- Captured piece tracking for both sides
- Pawn promotion
- Finished games saved to Firestore, with a per-player match history

## Stack

| Part | Choice |
|---|---|
| Backend | Python, FastAPI, WebSockets |
| Chess rules | `python-chess` |
| Auth | Google Identity Services + `google-auth` |
| Database | Cloud Firestore (`firebase-admin`) |
| Frontend | Single HTML file — React 18 via CDN, Tailwind |

The frontend is deliberately one file with no build step, so the project can be
run with two commands and no `npm install`.

## How it works

**Connecting.** The browser opens a WebSocket to `/ws` and sends the Google ID
token it received from the sign-in button. The server verifies that token
against its own client ID before trusting any identity claim — the name and
email come out of the verified token, never from what the client typed.

**Matchmaking.** A single `waiting_player` slot holds whoever arrived first.
The next player to connect is paired with them: first arrival plays white,
second plays black. The check-and-claim is wrapped in an `asyncio.Lock` so two
simultaneous connections cannot both match against the same waiting player.

**Playing.** The client sends a move in UCI form, e.g. `e2e4`. The server checks
that it is that player's turn, that the string parses as a move, and that the
move is legal in the current position. Only then does the board change. Both
players are then sent the new state — each with their own list of legal moves,
since the two sides see different options.

**Finishing.** After every move the server asks `python-chess` whether the game
has ended. If it has, both players are told the result and the completed game is
written to Firestore.

**Concurrency.** Each room has its own lock so two moves cannot interleave and
corrupt the board. The Firestore client is blocking, so every database call runs
through `asyncio.to_thread` — otherwise one slow write would stall the event
loop for every other game on the server.

## Running it locally

### 1. Install dependencies

```bash
uv add fastapi "uvicorn[standard]" python-chess google-auth python-dotenv firebase-admin
```

Or with pip:

```bash
pip install fastapi "uvicorn[standard]" python-chess google-auth python-dotenv firebase-admin
```

### 2. Set up Google sign-in

1. Go to the [Google Cloud Console credentials page](https://console.cloud.google.com/apis/credentials)
2. Create an **OAuth client ID** of type **Web application**
3. Under **Authorized JavaScript origins**, add `http://localhost:3000`
4. Copy the client ID it gives you

Create a `.env` file next to `main.py`:

```
GOOGLE_CLIENT_ID=your-client-id-here.apps.googleusercontent.com
```

### 3. Set up Firestore (optional)

The app runs fine without a database — you just lose match history.

1. Create a project at the [Firebase console](https://console.firebase.google.com)
2. Enable **Firestore Database**
3. Go to **Project settings → Service accounts → Generate new private key**
4. Save the downloaded file as `serviceAccountKey.json` next to `main.py`

The match history query needs one composite index. The first time you request
it, the server logs an error containing a link that creates the index for you —
or create it manually on the `games` collection:

| Field | Type |
|---|---|
| `player_ids` | Arrays |
| `finished_at` | Descending |

### 4. Run it

Two terminals:

```bash
# Terminal 1 — backend
uv run main.py

# Terminal 2 — frontend
python -m http.server 3000
```

Open `http://localhost:3000` in two browser windows, sign in with two different
Google accounts, and you will be matched together.

## Project layout

```
main.py                 backend: server, matchmaking, rules, database
index.html              frontend: board, lobby, match history
.env                    your Google client ID (not committed)
serviceAccountKey.json  your Firebase key (not committed)
```

## Known limitations

- **Single process only.** The waiting player and active rooms live in memory,
  so the app cannot run across multiple server instances. Moving that state to
  Redis would be the first step toward scaling.
- **No automated tests.** The move validation is the part most worth testing —
  illegal moves rejected, out-of-turn moves rejected, checkmate detected.
- **No reconnection.** If a player's connection drops mid-game, the game ends
  rather than letting them rejoin.
- **Game history is not access-controlled.** The history endpoint returns games
  for any player ID passed to it. Verifying the requester's token before
  returning results would fix this.

## Possible next steps

- Replay a finished game move by move — the full move list is already stored
- Win/loss records and a leaderboard
- Rating-based matchmaking instead of first-come-first-served
- Move clocks
