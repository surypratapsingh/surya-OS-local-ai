#!/usr/bin/env python3
"""Generate the N4 training and development sets from templates (stdlib only).

Run from the repository root:

    python3 intent/data/generate.py

Writes intent/data/train.tsv and intent/data/dev.tsv. Output is fixed for a
fixed seed and Python's `random` module (docs/work-orders.md N4).

Each line is `intent<TAB>text<TAB>tags`: one BIO tag per token, where tokens
follow the shared rule in intent/README.md ("Tokens"): lower-case ASCII, then
maximal runs of a-z and 0-9; every other character separates tokens. The
trainer re-tokenizes `text` and refuses a line whose token count differs from
its tag count, so this file and the Rust tokenizer check each other.

The hand-written test set (intent/data/test.jsonl) was committed before this
file existed and is never read here. Template phrasings were written by the
same author as that test set; the training log reports how many test lines
appear verbatim in train.tsv, and the score without them.
"""

from __future__ import annotations

from pathlib import Path
import random
import re

OUT = Path(__file__).resolve().parent
TOKEN = re.compile(r"[a-z0-9]+")

INTENTS = (
    "chat", "apps.open", "files.find", "files.open", "media.play_pause", "media.next",
    "media.previous", "volume.up", "volume.down", "volume.mute", "system.lock", "time.now",
    "timer.set", "memory.save",
)


def tokens(text: str) -> list[str]:
    """The shared rule: ASCII lower-case, then runs of [a-z0-9]."""
    lowered = "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)
    return TOKEN.findall(lowered)


# --- slot values ---------------------------------------------------------------

APP_NAMES = sorted(set(
    "spotify vlc notepad calculator calc chrome firefox edge brave opera safari vscode code word excel "
    "powerpoint outlook onenote teams zoom skype slack discord telegram whatsapp signal steam minecraft roblox "
    "paint gimp krita blender inkscape audacity obs premiere photoshop lightroom terminal cmd powershell bash "
    "konsole explorer files finder nautilus settings camera photos gallery music mail calendar clock alarms notes "
    "anki obsidian notion evernote kicad pycharm intellij eclipse atom libreoffice writer impress okular evince "
    "acrobat browser editor gedit kate vim emacs thunderbird virtualbox docker postman figma canva itunes winamp "
    "rhythmbox kodi plex netflix youtube chess solitaire minesweeper recorder maps weather news".split()
) | {
    "vs code", "visual studio", "visual studio code", "obs studio", "file explorer", "file manager",
    "control panel", "task manager", "system monitor", "command prompt", "sticky notes", "music player",
    "media player", "video player", "text editor", "web browser", "pdf reader", "adobe reader",
    "arduino ide", "android studio", "sublime text", "davinci resolve", "epic games", "prime video",
    "youtube music", "snipping tool", "voice recorder", "paint 3d", "google chrome", "microsoft word",
    "microsoft excel", "ms word", "ms excel", "libreoffice writer", "apple music", "windows terminal",
})

QUERIES = sorted(set("""physics notes|chapter 3|chapter 7 notes|budget 2026|budget|resume|cv|invoice|invoice 17
tax return|holiday photos|maths homework|math homework|chemistry lab report|lab report|project proposal
assignment 5|lecture slides|week 3 slides|syllabus|timetable|exam timetable|passport scan|report|essay draft
notes|song list|recipe|pasta recipe|birthday video|wedding photos|bank statement|salary slip|electricity bill
rent agreement|offer letter|thesis|thesis draft|presentation|flashcards|exam schedule|class 12 physics
organic chemistry notes|calculus notes|trigonometry worksheet|biology diagrams|history essay|english poem
admission form|fee receipt|marksheet|certificate|internship report|cover letter|meeting minutes|grocery list
travel itinerary|train ticket|boarding pass|insurance policy|medical report|prescription|warranty card
user manual|guitar chords|piano sheet music|workout plan|diet chart|reading list|book summary|research paper
lab manual|practical file|question paper|sample paper|answer key|study plan|revision notes|formula sheet
periodic table|map of india|family photos|school photos|college id|aadhaar card|pan card|driving licence
car insurance|phone bill|wifi bill|loan documents|project report|final report|annual report|sales data
attendance sheet|time table|class notes|lecture 4|module 2 notes|unit 3 notes|chapter 12 summary""".replace("\n", "|").split("|")))

LABELS = sorted(set("""stretch|drink water|check the oven|take a break|call mom|call dad|leave for class|take my medicine
check the rice|start revising|the pasta|laundry|the meeting|tea|workout|turn off the stove|feed the cat
water the plants|take the cake out|join the call|go to sleep|study physics|walk the dog|pick up my sister
send the email|submit the assignment|check the washing machine|move the car|eggs|the eggs|coffee|lunch
stand up|rest my eyes|start homework|leave for the bus|call the doctor|charge my phone|switch off the geyser""".replace("\n", "|").split("|")))

NOTES = sorted(set("""my exam is on Monday|the wifi password is on the router|Mom's birthday is June 5|I parked on level 2
the library closes at 8|the physics test covers optics|buy milk on friday|the meeting moved to Friday
chapter 4 is not in the syllabus|I owe Ravi 200 rupees|my locker code is 4312|dentist appointment next Tuesday at 4
the bus leaves at 7:15|my bike key is in the drawer|the project is due on the 20th|Priya's number ends with 4521
the plumber comes on Saturday|I lent my charger to Aman|the rent is due on the 1st|my roll number is 23
the gas cylinder was booked today|the maths tutor charges 500 per class|my passport expires in March
the spare key is under the pot|I need to renew my library card|the doctor said to drink more water
my blood group is B positive|the train is at platform 3|the password hint is my first school
the chemistry practical is on Wednesday|grandma likes jasmine tea|the shoe size I need is 9
I promised to call Neha back|the cricket match starts at 3|the refund will come in 5 days
the cat's vaccine is due in May|Sunday is the family dinner|my coach wants me there at 6
the router admin password is on the box|my seat is 14B|the parcel will arrive tomorrow|my next haircut is due
the lab coat is in the cupboard|exam hall is room 204|the charger is in my bag|I left my umbrella at Rahul's""".replace("\n", "|").split("|")))

NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
    10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen",
    17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
}


def number_word(n: int) -> str:
    if n in NUMBER_WORDS:
        return NUMBER_WORDS[n]
    tens, ones = divmod(n, 10)
    return f"{NUMBER_WORDS[tens * 10]} {NUMBER_WORDS[ones]}"


def duration(rng: random.Random) -> str:
    """A spoken duration; the runtime parses it back (novacore/src/novacore/intent.py)."""
    roll = rng.random()
    if roll < 0.12:
        return rng.choice(["an hour", "half an hour", "one hour", "a minute", "an hour and a half", "two hours",
                           "1 hour", "2 hours", "3 hours", "1 hr", "2 hrs", "a quarter of an hour"])
    minutes = rng.choice([1, 2, 3, 4, 5, 5, 7, 8, 10, 10, 12, 15, 15, 20, 20, 25, 30, 35, 40, 45, 45, 50, 55, 60, 90, 120])
    unit = rng.choice(["minutes", "minutes", "minutes", "mins", "min", "minute"] if minutes != 1 else ["minute", "min"])
    if minutes > 1 and unit == "minute" and rng.random() < 0.5:
        unit = "minutes"
    if minutes < 60 and rng.random() < 0.3:
        return f"{number_word(minutes)} {unit}"
    return f"{minutes} {unit}"


# --- templates -------------------------------------------------------------------
# {app} {query} {num} {label} {text} are slots; everything else is tagged O.

TEMPLATES: dict[str, list[str]] = {
    "apps.open": [
        "open {app}", "launch {app}", "start {app}", "run {app}", "fire up {app}", "bring up {app}",
        "open up {app}", "open the {app} app", "launch the {app} application", "start up {app}",
        "i want to use {app}", "get {app} running", "load {app}", "open {app} app", "start the {app} program",
        "boot up {app}", "can you launch {app}", "switch to {app}", "go to {app}", "show {app}",
        "open {app} for me", "i need {app}", "start {app} please", "open the {app}", "run the {app} app",
    ],
    "files.find": [
        "find {query}", "find my {query}", "search for {query}", "look for {query}", "where is my {query}",
        "where are my {query}", "locate {query}", "locate my {query}", "search my files for {query}",
        "find files named {query}", "find the file {query}", "list files about {query}", "find the {query}",
        "do i have a file called {query}", "search {query} in my documents", "find documents about {query}",
        "look for my {query}", "where did i save {query}", "where did i put my {query}", "search for my {query}",
        "list files with {query} in the name", "show files matching {query}", "any files called {query}",
        "search the folder for {query}", "find every file with {query}", "is there a file named {query}",
    ],
    "files.open": [
        "open the file {query}", "open my {query}", "open {query} pdf", "open the document {query}",
        "open the {query} file", "open my {query} file", "show me the {query} file", "pull up the {query}",
        "pull up my {query}", "open the {query} document", "open {query} file", "can you open my {query}",
        "open the document called {query}", "open the file called {query}", "show me my {query}",
        "open the {query} pdf", "display my {query}", "open up my {query}", "read my {query} file",
        "open the {query}", "bring up my {query} document", "open {query}",
    ],
    "media.play_pause": [
        "pause", "pause the music", "pause music", "play", "resume", "resume the music", "resume playback",
        "play music", "continue playing", "stop the music", "pause the video", "play the song",
        "unpause", "hit play", "hold the music", "pause the song", "pause playback", "resume the song",
        "play some music", "start the music again", "continue the music", "stop the song", "pause it",
        "play it", "keep playing", "continue playing the song", "pause the movie", "resume the video",
    ],
    "media.next": [
        "next song", "next track", "skip", "skip this song", "skip track", "play the next song",
        "go to the next track", "next", "change the song", "skip ahead", "skip this one", "next one",
        "play the next track", "move to the next song", "skip to the next song", "next song please",
        "i don't like this song skip it", "another song", "skip this track", "go to the next song",
    ],
    "media.previous": [
        "previous song", "previous track", "go back a song", "go back a track", "play the previous track",
        "back one track", "replay the previous song", "previous", "go to the previous song",
        "play the last track again", "back to the previous song", "previous one", "go back to the last song",
        "previous track please", "rewind to the previous song", "play the song before this",
    ],
    "volume.up": [
        "volume up", "turn up the volume", "turn the volume up", "turn it up", "louder", "make it louder",
        "increase the volume", "raise the volume", "crank it up", "turn the sound up", "more volume",
        "boost the volume", "increase volume", "a bit louder", "louder please", "pump up the volume",
        "turn it up a bit", "up the volume", "can you make it louder", "i can barely hear it turn it up",
    ],
    "volume.down": [
        "volume down", "turn down the volume", "turn the volume down", "turn it down", "quieter",
        "make it quieter", "lower the volume", "decrease the volume", "too loud", "it is too loud",
        "reduce the volume", "softer", "a bit quieter", "lower it", "turn the sound down", "less volume",
        "bring the volume down", "softer please", "its too loud", "make it softer",
    ],
    "volume.mute": [
        "mute", "mute the sound", "unmute", "mute audio", "silence", "mute everything", "toggle mute",
        "turn off the sound", "sound off", "unmute the speakers", "mute the speakers", "mute it",
        "mute the volume", "silence please", "kill the sound", "turn the sound back on", "unmute audio",
        "mute the computer",
    ],
    "system.lock": [
        "lock", "lock my computer", "lock the screen", "lock my pc", "lock the laptop", "lock it",
        "lock the system", "lock my screen", "screen lock", "lock the computer", "lock my laptop",
        "i'm stepping away lock the screen", "lock the pc now", "lock up the computer", "lock the desktop",
        "lock this machine", "secure the screen", "lock my machine",
    ],
    "time.now": [
        "what time is it", "what's the time", "tell me the time", "time please", "current time",
        "what is the time now", "what's today's date", "what day is it", "what is the date today",
        "which day is today", "date and time", "what's the date", "what is the time", "the time please",
        "what time is it now", "what's the time right now", "today's date", "what date is it",
        "do you know the time", "can you tell me the time", "what day is today", "time check",
        "what is today's date", "what's the day today",
    ],
    "timer.set": [
        "set a timer for {num}", "timer {num}", "remind me in {num} to {label}", "set a {num} timer",
        "start a {num} timer for {label}", "in {num} remind me to {label}", "ping me in {num}",
        "count down {num}", "remind me to {label} in {num}", "{num} timer for {label}",
        "set a timer for {label} for {num}", "set timer {num}", "start a timer for {num}",
        "remind me after {num} to {label}", "alert me in {num}", "set a {num} timer for {label}",
        "timer for {num} please", "in {num} tell me to {label}", "give me a {num} timer",
        "remind me in {num}", "set an alarm for {num} from now", "start a countdown of {num}",
    ],
    "memory.save": [
        "remember that {text}", "remember {text}", "note that {text}", "make a note that {text}",
        "save a note {text}", "save this note {text}", "write down that {text}", "don't let me forget that {text}",
        "keep in mind that {text}", "jot down {text}", "add a note {text}", "please remember {text}",
        "store this {text}", "note {text}", "make a note {text}", "remember this {text}",
        "save a note that {text}", "note down that {text}", "can you remember that {text}",
        "write this down {text}", "add to my notes {text}", "put a note that {text}",
    ],
}

# Chat: questions and conversation, and hard negatives that share words with actions.
CHAT_FIXED = """what is photosynthesis|explain newtons third law|how are you|tell me a story|who painted the mona lisa
what is the capital of japan|help me plan my week|explain the pythagoras theorem|summarize world war two
write a poem about the sea|what should i cook today|i am feeling sad|good night|thank you so much
what are your features|what is your name|translate thank you into french|solve 3x - 5 = 10
why do leaves change colour|how far is the moon|give me motivation to study|suggest a name for my dog
will it be sunny on sunday|how does the heart pump blood|what is the difference between speed and velocity
recommend a movie|explain object oriented programming|what is 20 percent of 150|how do i make coffee
tell me about the taj mahal|what is artificial intelligence|quiz me on biology|i am so bored
what's your favourite food|how long is a football match|convert 10 kg to pounds|who invented the telephone
what does cpu stand for|write a letter to my principal|do you like movies|hello|hi nova|hey there
good morning|good evening|ok thanks|that's great|you are funny|can we talk|i need advice
what is the meaning of life|how do airplanes fly|what is gravity|tell me something interesting
what is a black hole|how do i improve my handwriting|teach me python|what is html
explain the water cycle|how many bones are in the human body|what is inflation|who is the president of india
how do i say sorry to a friend|is coffee bad for you|how much water should i drink|what is a prime number
explain democracy|what are the planets|how to learn faster|why is exercise important|what is an atom
can you explain fractions|what is a verb|help me write an essay|i passed my exam|i failed my test
how do i stay focused|what is a good morning routine|what happened in 1857|who was gandhi
explain climate change|how does the internet work|what is a virus|what is a noun|tell me a riddle
""".replace("\n", "|").split("|")

HARD_NEGATIVE_TEMPLATES = [
    # volume (geometry and other senses)
    "what is the volume of a {shape}", "how do you calculate the volume of a {shape}", "volume of a {shape} with side {n}",
    "find the volume of a {shape}", "why is my {device} volume so low", "the volume of {stuff} is too much",
    "what is the formula for the volume of a {shape}", "how loud is a jet engine",
    # open
    "how do i open a {thing} in {lang}", "what does open source mean", "is the {place} open on {day}",
    "how to open a {object}", "give me examples of open ended questions", "when does the {place} open",
    "what are open and closed circuits", "is it open today", "how do i open a bank account",
    # find (maths and life)
    "find the derivative of {expr}", "find x if {n}x = {m}", "find the area of a {shape2} of radius {n}",
    "how do i find my purpose", "find the roots of {expr}", "find the value of {expr}", "find the integral of {expr}",
    "help me find a good {thing2}", "how do i find a job", "find the mean of {n} {m} and {k}",
    # time
    "what time zone is {country} in", "what is the time complexity of {algo}", "how much time does {event}",
    "what time does the {place} close", "is time travel possible", "time is money meaning",
    "how long does it take to {activity}", "what time should i sleep",
    # lock
    "how do i lock a {object} without a key", "what is a deadlock", "explain the lock and key model",
    "what does lockdown mean", "i locked myself out", "how does a smart lock work",
    # media words
    "what is the next prime after {n}", "who was the previous {role}", "what does pause mean in music",
    "how do i mute someone on {platform}", "is play a verb", "what is the next step in {process}",
    "what is the play {play} about", "play a game with me", "the last song was great", "next time i will do better",
    "stop talking", "what is the previous question", "skip counting by {n}", "what does skip mean",
    "who sang the song {song}", "what is your favourite song", "recommend some music",
    # timer and set
    "how do i set up {software}", "what is set theory", "how does a timer work in {lang}", "set the table",
    "how many minutes are in a {period}", "remind me what {concept} is", "what is a reminder",
    "set a goal for me", "how do i set boundaries",
    # memory
    "remember when we talked about {concept}", "do you remember my name", "what do you remember about me",
    "i can't remember the formula for {concept}", "how can i remember things better",
    "note the difference between {concept} and {concept2}", "what is a musical note", "do you remember {concept}",
    "remember me", "i forgot my password",
    # apps
    "launch date of {mission}", "start a conversation", "how do i run a {distance} faster",
    "what is {app} used for", "is {app} better than {app2}", "{app} or {app2} which is better",
    "how does a {app} work", "what are the best settings for my {device}", "my computer is slow why",
    "how do i install {app}", "why does {app} keep crashing", "what is the latest version of {app}",
    "who made {app}", "is {app} free",
]

FILLS = {
    "shape": ["sphere", "cylinder", "cube", "cone", "prism", "pyramid", "cuboid"],
    "shape2": ["circle", "sphere", "semicircle"],
    "device": ["phone", "laptop", "tv", "speaker", "camera"],
    "stuff": ["work", "traffic", "data", "homework"],
    "thing": ["pdf", "file", "csv file", "json file", "text file"],
    "lang": ["python", "java", "javascript", "c", "excel"],
    "place": ["bank", "library", "shop", "mall", "post office", "museum", "school"],
    "day": ["sunday", "monday", "a holiday", "diwali"],
    "object": ["coconut", "jar", "door", "window", "bottle", "locker"],
    "expr": ["x squared", "sin x", "x^2 + 3x", "2x + 5", "e to the x", "log x", "x^3"],
    "n": ["2", "3", "4", "5", "7", "10"], "m": ["12", "20", "15", "9"], "k": ["8", "11", "6"],
    "thing2": ["book", "laptop", "school", "doctor", "recipe"],
    "country": ["india", "japan", "germany", "brazil", "the uk"],
    "algo": ["binary search", "quicksort", "merge sort", "bubble sort"],
    "event": ["light take to reach earth", "it take to boil an egg", "a bus take to delhi"],
    "activity": ["boil an egg", "learn guitar", "run 5 km", "read a book"],
    "role": ["prime minister", "president", "captain", "principal"],
    "platform": ["whatsapp", "instagram", "zoom", "discord"],
    "process": ["mitosis", "photosynthesis", "the experiment", "the recipe"],
    "play": ["hamlet", "macbeth", "romeo and juliet"],
    "song": ["shape of you", "bohemian rhapsody", "kesariya"],
    "software": ["python", "git", "my printer", "a router", "vs code"],
    "period": ["day", "week", "year"],
    "concept": ["photosynthesis", "kinetic energy", "physics", "gravity", "osmosis", "the cell"],
    "concept2": ["meiosis", "speed", "mass", "diffusion"],
    "mission": ["chandrayaan 3", "apollo 11", "mangalyaan"],
    "distance": ["mile", "marathon", "5k"],
    "app": ["chrome", "spotify", "excel", "word", "vlc", "telegram", "notepad", "firefox", "zoom", "steam"],
    "app2": ["firefox", "apple music", "sheets", "docs", "whatsapp", "edge"],
}

PREFIXES = ["", "", "", "", "please ", "hey nova ", "nova ", "can you ", "could you ", "would you ",
            "can you please ", "i want to ", "go ahead and ", "ok nova ", "okay ", "pls ", "hey "]
SUFFIXES = ["", "", "", "", " please", " for me", " now", " right now", " thanks", " thank you", " pls"]
ENDINGS = ["", "", "", "?", "!", "."]
# Prefixes that read naturally only before a verb phrase (not before "what time is it").
VERB_ONLY = {"i want to ", "go ahead and ", "can you ", "could you ", "would you ", "can you please "}


def fill_slot(rng: random.Random, slot: str) -> str:
    if slot == "app":
        return rng.choice(APP_NAMES)
    if slot == "query":
        return rng.choice(QUERIES)
    if slot == "num":
        return duration(rng)
    if slot == "label":
        return rng.choice(LABELS)
    if slot == "text":
        return rng.choice(NOTES)
    raise KeyError(slot)


PART = re.compile(r"\{(app|query|num|label|text)\}")


def render(rng: random.Random, template: str) -> tuple[str, list[str]]:
    """Fill a template; return the text and one BIO tag per shared-rule token."""
    pieces: list[tuple[str, str | None]] = []
    position = 0
    for match in PART.finditer(template):
        pieces.append((template[position:match.start()], None))
        pieces.append((fill_slot(rng, match.group(1)), match.group(1)))
        position = match.end()
    pieces.append((template[position:], None))
    text = "".join(piece for piece, _ in pieces)
    tags: list[str] = []
    for piece, slot in pieces:
        for index, _ in enumerate(tokens(piece)):
            tags.append("O" if slot is None else ("B-" if index == 0 else "I-") + slot)
    if len(tags) != len(tokens(text)):
        raise ValueError(f"template pieces do not tokenize independently: {template!r} -> {text!r}")
    return text, tags


def fill_free(rng: random.Random, template: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: rng.choice(FILLS[m.group(1)]), template)


def typo(rng: random.Random, text: str) -> str:
    """One keyboard slip inside a word of 4+ letters, never changing the token count."""
    words = [m for m in re.finditer(r"[A-Za-z]{4,}", text)]
    if not words:
        return text
    word = rng.choice(words)
    w = word.group(0)
    i = rng.randrange(1, len(w) - 1)
    kind = rng.randrange(3)
    if kind == 0:
        w = w[:i] + w[i + 1] + w[i] + w[i + 2:]
    elif kind == 1:
        w = w[:i] + w[i + 1:]
    else:
        w = w[:i] + w[i] + w[i:]
    return text[:word.start()] + w + text[word.end():]


def decorate(rng: random.Random, text: str, tags: list[str], intent: str) -> tuple[str, list[str]]:
    prefix = rng.choice(PREFIXES)
    if intent in {"time.now", "chat"} and prefix in VERB_ONLY:
        prefix = ""
    suffix = rng.choice(SUFFIXES) if intent != "chat" else rng.choice(["", "", "", " please", " nova"])
    ending = rng.choice(ENDINGS)
    full = prefix + text + suffix + ending
    full_tags = ["O"] * len(tokens(prefix)) + tags + ["O"] * len(tokens(suffix + ending))
    roll = rng.random()
    if roll < 0.15:
        full = full.capitalize()
    elif roll < 0.18:
        full = full.upper()
    elif roll < 0.25:
        full = full.title()
    if rng.random() < 0.06:
        full = typo(rng, full)
    if len(tokens(full)) != len(full_tags):
        raise ValueError(f"decoration changed the token count: {full!r}")
    return full, full_tags


def examples(rng: random.Random, count: int) -> list[tuple[str, str, list[str]]]:
    # Share of each kind: 40% chat (half of it hard negatives), the rest spread over the 13 actions.
    out: list[tuple[str, str, list[str]]] = []
    action_intents = [name for name in INTENTS if name != "chat"]
    for _ in range(count):
        roll = rng.random()
        if roll < 0.2:
            text, intent = rng.choice(CHAT_FIXED).strip(), "chat"
            tags = ["O"] * len(tokens(text))
        elif roll < 0.4:
            text, intent = fill_free(rng, rng.choice(HARD_NEGATIVE_TEMPLATES)), "chat"
            tags = ["O"] * len(tokens(text))
        else:
            intent = rng.choice(action_intents)
            text, tags = render(rng, rng.choice(TEMPLATES[intent]))
        if not tokens(text):
            continue
        text, tags = decorate(rng, text, tags, intent)
        out.append((intent, text, tags))
    return out


def write(path: Path, rows: list[tuple[str, str, list[str]]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for intent, text, tags in rows:
            assert "\t" not in text and "\n" not in text
            handle.write(f"{intent}\t{text}\t{' '.join(tags)}\n")


def main() -> None:
    write(OUT / "train.tsv", examples(random.Random(20261031), 24_000))
    write(OUT / "dev.tsv", examples(random.Random(31102026), 3_000))


if __name__ == "__main__":
    main()
