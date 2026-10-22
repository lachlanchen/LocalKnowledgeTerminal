# Shareable terminal views

The browser address updates as you change tabs, cards, graph branches, and
Q&A slides. Paste that address to report the view you are seeing.

## Tab links

Using the current Pi address:

- Questions: http://192.168.70.131:8090/?mode=question
- Answers: http://192.168.70.131:8090/?mode=answer
- Word Card: http://192.168.70.131:8090/?mode=knowledge
- Word Origin: http://192.168.70.131:8090/?mode=word
- Roots: http://192.168.70.131:8090/?mode=root
- Affixes: http://192.168.70.131:8090/?mode=affix
- Model Lab: http://192.168.70.131:8090/?mode=chat

## Exact saved views

The address adds a saved card ID and, where applicable:

- `slide`: one-based graph branch or Q&A slide position.
- `focus`: graph focus ID, preferred over the position if it still exists.
- `entity`: a graph node selected as a new center.
- `language`: the alternate language currently shown on a Word Card.

A link containing `card` opens paused. Press Play to resume the display loop.
The bare homepage retains its cross-tab loop; `display` kiosk links retain
autoplay. Unrelated query parameters and fragments are preserved. URLs use
native browser APIs with no routing library, page reload, or autoplay history
flood.

Opening a link only reads saved data. It never requests model generation.
A previously accepted card remains linkable after it is superseded, using
`GET /api/cards/{id}?revision=1`. The normal API and carousel still expose only
current accepted cards. Candidates, rejected cards, and archived/withdrawn
revisions are not made public.

Links identify saved cards, not frozen database snapshots: graph hydration can
reflect later accepted knowledge. If a branch no longer exists or the viewport
uses different branch pagination, its slide position is clamped to the current
view. Local language visibility settings also remain in effect. A removed or
withdrawn card displays an error without silently generating a replacement.
