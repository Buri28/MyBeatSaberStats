import urllib.request, json
url = "https://api.accsaberreloaded.com/v1/maps?page=0&size=50&categoryId=b0000000-0000-0000-0000-000000000002&search=astrid"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req) as r:
    data = json.load(r)
for song in data["content"]:
    for d in song.get("difficulties", []):
        print(f"song={song['songName']} diff={d['difficulty']} blId={d.get('blLeaderboardId')} ssId={d.get('ssLeaderboardId')}")
