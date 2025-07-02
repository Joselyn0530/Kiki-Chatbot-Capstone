import os
import dialogflow_v2 as dialogflow
from google.cloud import firestore
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# Initialize Firestore DB
# Ensure GOOGLE_APPLICATION_CREDENTIALS environment variable is set in Netlify
db = firestore.Client()

@app.route('/.netlify/functions/kiki_webhook', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)

    print(f"Dialogflow Request: {req}")

    # Extract intent display name
    intent_display_name = req.get('queryResult', {}).get('intent', {}).get('displayName')
    print(f"Intent Display Name: {intent_display_name}")

    if intent_display_name == 'set.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')
        date_time_str = parameters.get('date-time')

        # Get user client ID from custom payload
        user_client_id = req.get('queryResult', {}).get('queryParams', {}).get('payload', {}).get('user_client_id')
        print(f"User Client ID: {user_client_id}")

        if not task or not date_time_str:
            print("Missing task or date-time parameter.")
            return jsonify({
                "fulfillmentText": "I'm sorry, I couldn't understand the task or time for the reminder. Could you please specify it again?"
            })

        try:
            # Parse the date-time string from Dialogflow
            # It comes in ISO 8601 format like "2025-07-03T01:45:00+08:00"
            # Parse with timezone awareness
            reminder_dt_obj = datetime.fromisoformat(date_time_str)

            # Firestore automatically handles timezone-aware datetimes as Timestamps
            # No explicit conversion to UTC needed here, Firestore stores it as-is and correctly
            # provides it back as a Timestamp object.

            reminder_timestamp = reminder_dt_obj # This is a datetime object, Firestore will convert it

            # Save to Firestore
            reminder_data = {
                'task': task,
                'remind_at': reminder_timestamp,
                'user_client_id': user_client_id, # Save the client ID
                'status': 'pending',
                'created_at': firestore.SERVER_TIMESTAMP # Use server timestamp for creation
            }
            db.collection('reminders').add(reminder_data)
            print(f"Reminder saved to Firestore: {reminder_data}")

            # Format the time for the user-friendly response
            # Display time in local timezone for the user if needed, or keep ISO for clarity
            # For user display, you might want to format it nicely:
            user_friendly_time_str = reminder_dt_obj.strftime("%I:%M %p on %B %d, %Y") # e.g., "01:45 AM on July 03, 2025"

            # Construct the response for Dialogflow
            response = {
                "fulfillmentMessages": [
                    {"text": {"text": [f"Got it! I'll remind you to '{task}' at {user_friendly_time_str}."]}},
                    {"text": {"text": ["Reminder placed successfully."]}
                    # REMOVE OR COMMENT OUT THIS LINE IF IT'S PRESENT
                    # {"text": {"text": [f"{task}|{reminder_timestamp}"]}}
                    }
                ]
                # If you prefer a simpler fulfillmentText (single message displayed):
                # "fulfillmentText": f"Got it! I'll remind you to '{task}' at {user_friendly_time_str}. Reminder placed successfully."
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

    # Add other intent handlers here if you have them
    # For any other intent, you might just return a simple response
    return jsonify({
        "fulfillmentText": "This is a default response. Please make sure your Dialogflow intents are set up to use webhook fulfillment for specific actions."
    })


if __name__ == '__main__':
    # This is for local testing only. Render (and Netlify Functions) run as handlers.
    # To test locally, set GOOGLE_APPLICATION_CREDENTIALS environment variable
    # and run 'flask run' or 'python kiki_webhook.py'
    port = int(os.environ.get("PORT", 8000)) # Use Render's PORT env var
    app.run(host='0.0.0.0', port=port, debug=True)