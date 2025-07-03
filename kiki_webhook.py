import os
import json
import tempfile
from google.cloud import firestore
from flask import Flask, request, jsonify
from datetime import datetime
import re

# --- START CREDENTIALS SETUP FOR RENDER ---
firebase_key_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY')
temp_credentials_path = None

if firebase_key_json:
    try:
        fd, path = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w') as tmp:
            tmp.write(firebase_key_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        temp_credentials_path = path
        print(f"Temporary credentials file created at: {path}")
    except Exception as e:
        print(f"Error setting up credentials from environment variable: {e}")
else:
    print("FIREBASE_SERVICE_ACCOUNT_KEY not found. Using local credentials.")
# --- END CREDENTIALS SETUP FOR RENDER ---

app = Flask(__name__)
db = firestore.Client()

@app.route('/', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)
    print(f"Dialogflow Request: {json.dumps(req, indent=2)}")

    intent_display_name = req.get('queryResult', {}).get('intent', {}).get('displayName')
    print(f"Intent Display Name: {intent_display_name}")

    if intent_display_name == 'set.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')
        date_time_str = parameters.get('date-time')

        # --- TRY extracting user_client_id from payload first ---
        user_client_id = req.get('originalDetectIntentRequest', {}).get('payload', {}).get('user_client_id')
        
        # --- FALLBACK: Extract from queryText (e.g., for dev testing) ---
        if not user_client_id:
            query_text = req.get('queryResult', {}).get('queryText', '')
            match = re.search(r'--CLIENT_ID:([a-f0-9-]+)', query_text)
            if match:
                user_client_id = match.group(1)


        print(f"Extracted user_client_id: {user_client_id}")

        # --- Validation ---
        if not task or not date_time_str:
            print("Missing task or date-time.")
            return jsonify({
                "fulfillmentText": "I couldn't understand the task or time for the reminder. Please try again?"
            })

        if not user_client_id:
            print("Missing user_client_id.")
            return jsonify({
                "fulfillmentText": "I encountered an issue identifying you to save the reminder. Please try again from the web page!"
            })

        try:
            # Firestore handles tz-aware timestamps correctly
            reminder_dt = datetime.fromisoformat(date_time_str)

            reminder_data = {
                'task': task,
                'remind_at': reminder_dt,
                'user_client_id': user_client_id,
                'status': 'pending',
                'created_at': firestore.SERVER_TIMESTAMP
            }

            db.collection('reminders').add(reminder_data)
            print("Reminder stored:", reminder_data)

            response = {
                "fulfillmentMessages": [
                    {
                        "text": {
                            "text": [
                                f"Got it! I'll remind you to '{task}' at {reminder_dt.strftime('%I:%M %p on %B %d, %Y')}."
                            ]
                        }
                    }
                ]
            }
            return jsonify(response)

        except ValueError as e:
            print(f"Date parsing error: {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding the time. Please try a format like 'tomorrow at 3 PM'."
            })
        except Exception as e:
            print(f"Unexpected error: {e}")
            return jsonify({
                "fulfillmentText": "Something went wrong while saving your reminder. Please try again later."
            })

    return jsonify({
        "fulfillmentText": "I'm not sure how to respond to that yet. You can ask me to set a reminder!"
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=True)
