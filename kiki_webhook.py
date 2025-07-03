import os
import json
import tempfile
from google.cloud import dialogflow
from google.cloud import firestore
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

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
    print("FIREBASE_SERVICE_ACCOUNT_KEY environment variable not found. Relying on default credentials or local setup.")
# --- END CREDENTIALS SETUP FOR RENDER ---

app = Flask(__name__)
db = firestore.Client()

@app.route('/', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)
    print(f"Dialogflow Request: {req}")

    intent_display_name = req.get('queryResult', {}).get('intent', {}).get('displayName')
    print(f"Intent Display Name: {intent_display_name}")

    if intent_display_name == 'set.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')  # Correct as per latest log
        date_time_str = parameters.get('date-time') # Correct as per latest log

        # --- CORRECTED: Extract user_client_id from originalDetectIntentRequest.payload ---
        user_client_id = req.get('originalDetectIntentRequest', {}).get('payload', {}).get('user_client_id')
        print(f"User Client ID: {user_client_id}")

        if not task or not date_time_str:
            print("Missing task or date-time parameter.")
            return jsonify({
                "fulfillmentText": "I'm sorry, I couldn't understand the task or time for the reminder. Could you please specify it again?"
            })
        
        # Add a check for user_client_id for robustness
        if not user_client_id:
            print("User Client ID is missing, cannot save user-specific reminder.")
            return jsonify({
                "fulfillmentText": "I encountered an issue identifying you to save the reminder. Please try again from the web page!"
            })

        try:
            reminder_dt_obj = datetime.fromisoformat(date_time_str)

            reminder_data = {
                'task': task,
                'remind_at': reminder_dt_obj,
                'user_client_id': user_client_id,
                'status': 'pending',
                'created_at': firestore.SERVER_TIMESTAMP
            }
            db.collection('reminders').add(reminder_data)
            print(f"Reminder saved to Firestore: {reminder_data}")

            user_friendly_time_str = reminder_dt_obj.strftime("%I:%M %p on %B %d, %Y")

            response = {
                "fulfillmentMessages": [
                    {"text": {"text": [f"Got it! I'll remind you to '{task}' at {user_friendly_time_str}."]}
                    }
                ]
            }
            return jsonify(response)

        except ValueError as e:
            print(f"Date parsing error: {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding the time. Please use a clear format like 'tomorrow at 2 PM'."
            })
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            return jsonify({
                "fulfillmentText": "I'm sorry, something went wrong while trying to set your reminder. Please try again later."
            })

    return jsonify({
        "fulfillmentText": "I'm not sure how to respond to that yet. Please ask me about setting a reminder!"
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=True)
