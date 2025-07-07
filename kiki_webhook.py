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

    # Helper function to get context parameter
    # This checks both input and output contexts for robustness
    def get_context_parameter(context_name_part, param_name):
        # Check output contexts (active for the *next* turn)
        for context in req.get('queryResult', {}).get('outputContexts', []):
            # The context name can be long: projects/<project_id>/agent/sessions/<session_id>/contexts/context_name
            if context_name_part in context.get('name', ''):
                if param_name in context.get('parameters', {}):
                    return context['parameters'][param_name]
        # Check input contexts (active for the *current* turn)
        for context in req.get('queryResult', {}).get('inputContexts', []):
            if context_name_part in context.get('name', ''):
                if param_name in context.get('parameters', {}):
                    return context['parameters'][param_name]
        return None

    if intent_display_name == 'set.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')  
        date_time_str = parameters.get('date-time') 

        if not task or not date_time_str:
            print("Missing task or date-time parameter.")
            return jsonify({
                "fulfillmentText": "I'm sorry, I couldn't understand the task or time for the reminder. Could you please specify it again?"
            })
        
        try:
            reminder_dt_obj = datetime.fromisoformat(date_time_str)

            reminder_data = {
                'task': task,
                'remind_at': reminder_dt_obj,
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

    # Handle delete.reminder intent (initial request to find and confirm)
    elif intent_display_name == 'delete.reminder': 
        parameters = req.get('queryResult', {}).get('parameters', {})
        task_to_delete = parameters.get('task')
        date_time_to_delete_str = parameters.get('date-time')

        if not task_to_delete or not date_time_to_delete_str:
            return jsonify({
                "fulfillmentText": "To delete a reminder, please tell me the task and the approximate time, e.g., 'delete my bath reminder at 8 PM'."
            })

        try:
            target_dt_obj = datetime.fromisoformat(date_time_to_delete_str)
            
            # Create a small time window (e.g., +/- 1 minute) around the target time
            # to account for potential slight discrepancies in parsing or user input.
            time_window_start = target_dt_obj - timedelta(minutes=1)
            time_window_end = target_dt_obj + timedelta(minutes=1)

            # Query Firestore for pending reminders matching the task within the time window
            query = db.collection('reminders') \
                      .where('task', '==', task_to_delete) \
                      .where('status', '==', 'pending') \
                      .where('remind_at', '>=', time_window_start) \
                      .where('remind_at', '<=', time_window_end) \
                      .limit(1) # Limit to 1 for simplicity, assuming a unique enough match

            docs = query.get()

            if docs:
                reminder_doc = next(iter(docs)) # Get the first (and only) doc
                reminder_data = reminder_doc.to_dict()
                reminder_id = reminder_doc.id
                
                # Format reminder time for user-friendly display
                found_remind_at_dt = reminder_data['remind_at'].toDate()
                user_friendly_time_str = found_remind_at_dt.strftime("%I:%M %p on %B %d, %Y")
                
                # Get the current session to set context for the next turn
                session_id = req['session']
                
                # Response with confirmation question and context parameters
                response = {
                    "fulfillmentText": f"I found your reminder to '{reminder_data['task']}' at {user_friendly_time_str}. Do you want me to delete it?",
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                            "lifespanCount": 2, # Context active for 2 turns
                            "parameters": {
                                "reminder_id_to_delete": reminder_id,
                                "reminder_task_found": reminder_data['task'],
                                "reminder_time_found": user_friendly_time_str
                            }
                        }
                    ]
                }
                print(f"Found reminder {reminder_id} for deletion. Awaiting confirmation.")
                return jsonify(response)
            else:
                print(f"No pending reminder found for task '{task_to_delete}' around {target_dt_obj}.")
                user_friendly_time_str = target_dt_obj.strftime("%I:%M %p on %B %d, %Y")
                return jsonify({
                    "fulfillmentText": f"I couldn't find a pending reminder to '{task_to_delete}' around {user_friendly_time_str}. Please make sure the task and time are correct and it's still pending."
                })

        except ValueError as e:
            print(f"Date parsing error in delete: {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding the time. Please use a clear format like 'tomorrow at 2 PM'."
            })
        except Exception as e:
            print(f"An unexpected error occurred during reminder lookup for deletion: {e}")
            return jsonify({
                "fulfillmentText": "I'm sorry, something went wrong while trying to find your reminder for deletion. Please try again later."
            })

    # Handle delete.reminder - yes intent (confirmation step)
    elif intent_display_name == 'delete.reminder - yes': # This assumes your Dialogflow follow-up intent is named exactly this
        # Retrieve reminder details from the active context
        reminder_id_to_delete = get_context_parameter('awaiting_deletion_confirmation', 'reminder_id_to_delete')
        reminder_task_found = get_context_parameter('awaiting_deletion_confirmation', 'reminder_task_found')
        reminder_time_found = get_context_parameter('awaiting_deletion_confirmation', 'reminder_time_found')

        if reminder_id_to_delete:
            try:
                db.collection('reminders').document(reminder_id_to_delete).delete()
                print(f"Reminder (ID: {reminder_id_to_delete}) confirmed and deleted from Firestore.")
                return jsonify({
                    "fulfillmentText": f"Okay, I've successfully deleted your reminder to '{reminder_task_found}' at {reminder_time_found}."
                })
            except Exception as e:
                print(f"Error deleting reminder from context: {e}")
                return jsonify({
                    "fulfillmentText": "I'm sorry, I encountered an error while trying to delete the reminder. Please try again."
                })
        else:
            print("No reminder ID found in context for deletion confirmation.")
            return jsonify({
                "fulfillmentText": "I'm sorry, I lost track of which reminder you wanted to delete. Please tell me again."
            })

    return jsonify({
        "fulfillmentText": "I'm not sure how to respond to that yet. Please ask me about setting a reminder!"
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=True)
