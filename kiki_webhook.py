import os
import json
import tempfile
from google.cloud import dialogflow
from google.cloud import firestore
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

# --- START CREDENTIALS SETUP FOR RENDER ---
# Read the JSON content of your service account key from the environment variable.
# Ensure you set FIREBASE_SERVICE_ACCOUNT_KEY on Render's dashboard.
firebase_key_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY')
temp_credentials_path = None # Initialize to store path if a temp file is created

if firebase_key_json:
    try:
        # Create a temporary file to store the credentials.
        # This is necessary because google-cloud-firestore expects a file path,
        # not the raw JSON content directly in GOOGLE_APPLICATION_CREDENTIALS.
        fd, path = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w') as tmp:
            tmp.write(firebase_key_json)

        # Set the GOOGLE_APPLICATION_CREDENTIALS environment variable
        # to point to this temporary file's path.
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        temp_credentials_path = path # Store path for potential cleanup (though not strictly needed in Render)
        print(f"Temporary credentials file created at: {path}")

    except Exception as e:
        print(f"Error setting up credentials from environment variable: {e}")
        # In a production application, you might want to handle this error
        # more robustly, e.g., by logging a critical error or exiting.
else:
    print("FIREBASE_SERVICE_ACCOUNT_KEY environment variable not found. Relying on default credentials or local setup.")
# --- END CREDENTIALS SETUP FOR RENDER ---

# Initialize Flask app
app = Flask(__name__)

# Initialize Firestore DB.
# This will now use the credentials provided via GOOGLE_APPLICATION_CREDENTIALS.
db = firestore.Client()

# Define the webhook endpoint.
# Using '/' means your Render service's base URL will be the endpoint
# (e.g., https://your-service.onrender.com/).
# If you prefer a specific path like '/webhook', change it here: @app.route('/webhook', methods=['POST'])
@app.route('/', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)

    print(f"Dialogflow Request: {req}")

    # Extract intent display name from the Dialogflow request
    intent_display_name = req.get('queryResult', {}).get('intent', {}).get('displayName')
    print(f"Intent Display Name: {intent_display_name}")

    # Handle the 'set.reminder' intent
    if intent_display_name == 'set.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')
        date_time_str = parameters.get('date-time')

        # Get user client ID from the custom payload sent by Dialogflow Messenger's frontend
        user_client_id = req.get('queryParams', {}).get('payload', {}).get('user_client_id')
        print(f"User Client ID: {user_client_id}")

        # Validate required parameters
        if not task or not date_time_str:
            print("Missing task or date-time parameter.")
            return jsonify({
                "fulfillmentText": "I'm sorry, I couldn't understand the task or time for the reminder. Could you please specify it again?"
            })

        try:
            # Parse the date-time string from Dialogflow (ISO 8601 format: "YYYY-MM-DDTHH:MM:SS[+HH:MM]")
            reminder_dt_obj = datetime.fromisoformat(date_time_str)

            # Prepare data to save to Firestore
            reminder_data = {
                'task': task,
                'remind_at': reminder_dt_obj, # Firestore automatically converts datetime objects to Timestamps
                'user_client_id': user_client_id, # Link reminder to a specific user
                'status': 'pending', # Initial status
                'created_at': firestore.SERVER_TIMESTAMP # Use Firestore's server timestamp for creation
            }
            # Add the reminder to the 'reminders' collection in Firestore
            db.collection('reminders').add(reminder_data)
            print(f"Reminder saved to Firestore: {reminder_data}")

            # Format the time for a user-friendly response (e.g., "01:45 AM on July 03, 2025")
            user_friendly_time_str = reminder_dt_obj.strftime("%I:%M %p on %B %d, %Y")

            # Construct the response for Dialogflow Messenger.
            # This will only contain the user-friendly message, no raw data.
            response = {
                "fulfillmentMessages": [
                    {"text": {"text": [f"Got it! I'll remind you to '{task}' at {user_friendly_time_str}."]}
                    }
                ]
            }
            return jsonify(response)

        except ValueError as e:
            # Handle errors during date parsing
            print(f"Date parsing error: {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding the time. Please use a clear format like 'tomorrow at 2 PM'."
            })
        except Exception as e:
            # Handle any other unexpected errors during reminder setup
            print(f"An unexpected error occurred: {e}")
            return jsonify({
                "fulfillmentText": "I'm sorry, something went wrong while trying to set your reminder. Please try again later."
            })

    # Default response if the intent is not 'set.reminder'
    # You can add more 'elif' blocks here for other intents you want to handle via webhook.
    return jsonify({
        "fulfillmentText": "I'm not sure how to respond to that yet. Please ask me about setting a reminder!"
    })

# This block is for local development and testing only.
# Render (via Gunicorn) will run the 'app' instance directly.
if __name__ == '__main__':
    # Render provides a PORT environment variable, use it if available, otherwise default to 8000.
    port = int(os.environ.get("PORT", 8000))
    # '0.0.0.0' allows the Flask development server to be accessible from outside the local machine,
    # which is necessary in containerized environments like Render.
    app.run(host='0.0.0.0', port=port, debug=True)

# Note: The temporary file created for credentials is typically cleaned up
# when the Render container/process restarts or is destroyed.
