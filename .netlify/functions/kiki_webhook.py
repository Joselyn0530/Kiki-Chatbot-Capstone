import json
import os
from datetime import datetime, timezone
import pytz # For robust timezone handling
from google.cloud import firestore

# Initialize Firestore client
# Netlify will automatically use GOOGLE_APPLICATION_CREDENTIALS_JSON env var
db = firestore.Client()

def handler(event, context):
    try:
        dialogflow_request = json.loads(event['body'])
        intent_name = dialogflow_request['queryResult']['intent']['displayName']
        
        # Extract user_client_id from the custom payload we send from app.js
        user_client_id = dialogflow_request.get('queryResult', {}).get('queryParams', {}).get('payload', {}).get('user_client_id')
        
        if not user_client_id:
            # Fallback to session ID if custom user_client_id is not found
            # This is less reliable for persistence across sessions, but better than nothing
            user_client_id = dialogflow_request['session'].split('/')[-1]
            print(f"Warning: user_client_id not found in payload, using session ID: {user_client_id}")

        fulfillment_text = "I'm sorry, I didn't understand that. Can you rephrase?"

        # --- Handle 'set.reminder' intent ---
        if intent_name == 'set.reminder':
            params = dialogflow_request['queryResult']['parameters']
            task = params.get('task')
            date_time_str = params.get('date-time') # e.g., "2025-07-03T10:00:00+08:00"

            if not task or not date_time_str:
                fulfillment_text = "Sorry, I need both the task and the time for the reminder."
            else:
                try:
                    # Parse the ISO format string provided by Dialogflow
                    remind_at_dt = datetime.fromisoformat(date_time_str)
                    # Convert to UTC for consistent storage in Firestore
                    remind_at_utc = remind_at_dt.astimezone(timezone.utc)

                    # Add reminder to Firestore
                    reminders_ref = db.collection('reminders')
                    new_reminder_doc = {
                        'task': task,
                        'remind_at': remind_at_utc,
                        'user_client_id': user_client_id, # Link to the specific browser
                        'status': 'pending', # Status for tracking (client-side will update to 'completed')
                        'created_at': datetime.now(timezone.utc)
                    }
                    reminders_ref.add(new_reminder_doc) 

                    # Format datetime for user-friendly response (optional, can be simplified)
                    # Example: "2025-07-03 at 10:00 AM" (adjust based on remind_at_dt's original timezone)
                    # For simplicity, let's just show what Dialogflow provided, or a basic UTC format
                    display_time = remind_at_dt.strftime('%Y-%m-%d %H:%M') # Use original local time
                    fulfillment_text = f"Alright, I've noted that. I'll alert you to '{task}' on {display_time} if you have my page open."

                except ValueError:
                    fulfillment_text = "I couldn't understand that date and time. Please try again with a clearer date or time."
                except Exception as e:
                    print(f"Error saving reminder to Firestore: {e}")
                    fulfillment_text = "There was an issue saving your reminder. Please try again."

        # Prepare Dialogflow response
        response = {
            "fulfillmentMessages": [
                {
                    "text": {
                        "text": [fulfillment_text]
                    }
                }
            ]
        }

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json'
            },
            'body': json.dumps(response)
        }

    except Exception as e:
        print(f"Unhandled error in webhook: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({"error": str(e)})
        }