
import json
import urllib.request
import urllib.error

URL = "http://127.0.0.1:8000/api/roster"


def test_api(filename):
    with open(filename, "r", encoding="utf-8") as file:
        data = json.load(file)

    request_data = json.dumps(data).encode("utf-8")

    request = urllib.request.Request(
        URL,`
        data=request_data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(request) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

            print("\nFile:", filename)
            print("HTTP Status:", response.status)
            print("Roster Status:", result.get("status"))
            print("Total Value:", result.get("value_total"))
            print("Fuel Used:", result.get("fuel_total_litres"))
            print("API test completed.")

    except urllib.error.HTTPError as error:
        print("\nFile:", filename)
        print("HTTP Error:", error.code)
        print(error.read().decode("utf-8"))

    except urllib.error.URLError:
        print("Server is not running. Start app.py first.")


if __name__ == "__main__":
    test_api("sample.json")
    test_api("minute150.json")
