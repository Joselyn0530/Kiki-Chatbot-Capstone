import os
import dialogflow_v2 as dialogflow
from google.cloud import firestore
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

# Initialize Flask app
app = Flask(__name__)

# Initialize Firestore DB
# Ensure GOOGLE_APPLICATION_CREDENTIALS environment variable is set on Render
db = firestore.Client()

# Define the webhook endpoint.
# It's common to use '/' for the root of the Render service,
# or '/webhook' if you prefer a specific path.
# Make sure this matches the URL you set in Dialogflow's Fulfillment settings.
@app.route('/', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)

    print(f"Dialogflow Request: {req}")

    # Extract intent display name
    intent_display_name = req.get('queryResult', {}).get('intent', {}).get('displayName')
    print(f"Intent Display Name: {intent_display_name}")

    # Handle the 'set.reminder' intent
    if intent_display_name == 'set.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')
        date_time_str = parameters.get('date-time')

        # Get user client ID from custom payload (sent from frontend app.js)
        user_client_id = req.get('queryResult', {}).get('queryParams', {}).get('payload', {}).get('user_client_id')
        print(f"User Client ID: {user_client_id}")

        if not task or not date_time_str:
            print("Missing task or date-time parameter.")
            return jsonify({
                "fulfillmentText": "I'm sorry, I couldn't understand the task or time for the reminder. Could you please specify it again?"
            })

        try:
            # Parse the date-time string from Dialogflow (ISO 8601 format: "YYYY-MM-DDTHH:MM:SS[+HH:MM]")
            reminder_dt_obj = datetime.fromisoformat(date_time_str)

            # Firestore automatically handles timezone-aware datetimes as Timestamps.
            # No explicit conversion to UTC is strictly needed here, Firestore will store it correctly.

            # Save to Firestore
            reminder_data = {
                'task': task,
                'remind_at': reminder_dt_obj, # Store as datetime object, Firestore converts to Timestamp
                'user_client_id': user_client_id, # Save the client ID
                'status': 'pending',
                'created_at': firestore.SERVER_TIMESTAMP # Use server timestamp for creation
            }
            db.collection('reminders').add(reminder_data)
            print(f"Reminder saved to Firestore: {reminder_data}")

            # Format the time for a user-friendly response (e.g., "01:45 AM on July 03, 2025")
            user_friendly_time_str = reminder_dt_obj.strftime("%I:%M %p on %B %d, %Y")

            # Construct the response for Dialogflow (without raw data)
            response = {
                "fulfillmentMessages": [
                    {"text": {"text": [f"Got it! I'll remind you to '{task}' at {user_friendly_time_str}."]}
                    }
                ]
                # If you prefer a single fulfillmentText string:
                # "fulfillmentText": f"Got it! I'll remind you to '{task}' at {user_friendly_time_str}."
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

    # Default response for other intents if no specific handler is found
    # You might want to add more intent handlers here for other chatbot functionalities.
    return jsonify({
        "fulfillmentText": "I'm not sure how to respond to that yet. Please ask me about setting a reminder!"
    })

# This block is for local development and testing.
# Render's server (Gunicorn) will handle running 'app' directly.
if __name__ == '__main__':
    # Render provides a PORT environment variable. Use it if available, otherwise default to 8000.
    port = int(os.environ.get("PORT", 8000))
    # '0.0.0.0' makes the server accessible from outside the local machine
    # (necessary in containerized environments like Render).
    app.run(host='0.0.0.0', port=port, debug=True)
