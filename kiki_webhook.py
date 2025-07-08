import os
import json
import tempfile
from google.cloud import dialogflow
from google.cloud import firestore
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone
import pytz

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

# Define the local timezone for display (Malaysia, GMT+8)
KUALA_LUMPUR_TZ = pytz.timezone('Asia/Kuala_Lumpur')

# Helper function to get context parameter
def get_context_parameter(req_payload, context_name_part, param_name):
    for context in req_payload.get('queryResult', {}).get('outputContexts', []):
        if context_name_part in context.get('name', ''):
            if param_name in context.get('parameters', {}):
                return context['parameters'][param_name]
    for context in req_payload.get('queryResult', {}).get('inputContexts', []):
        if context_name_part in context.get('name', ''):
            if param_name in context.get('parameters', {}):
                return context['parameters'][param_name]
    return None

@app.route('/', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)
    print(f"Dialogflow Request: {req}")

    intent_display_name = req.get('queryResult', {}).get('intent', {}).get('displayName')
    print(f"Intent Display Name: {intent_display_name}")

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

            user_friendly_time_str = reminder_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")

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
            time_window_start = target_dt_obj - timedelta(minutes=1)
            time_window_end = target_dt_obj + timedelta(minutes=1)

            query = db.collection('reminders') \
                      .where('task', '==', task_to_delete) \
                      .where('status', '==', 'pending') \
                      .where('remind_at', '>=', time_window_start) \
                      .where('remind_at', '<=', time_window_end) \
                      .limit(1)

            docs = query.get()

            if docs:
                reminder_doc = next(iter(docs)) 
                reminder_data = reminder_doc.to_dict()
                reminder_id = reminder_doc.id
                
                user_friendly_time_str = reminder_data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                
                session_id = req['session']
                
                response = {
                    "fulfillmentText": f"I found your reminder to '{reminder_data['task']}' at {user_friendly_time_str}. Do you want me to delete it?",
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                            "lifespanCount": 2, 
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
                user_friendly_time_str = target_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
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
    elif intent_display_name == 'delete.reminder - yes': 
        reminder_id_to_delete = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_id_to_delete')
        reminder_task_found = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_task_found')
        reminder_time_found = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_time_found')

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

    # Handle delete.reminder - no intent (negation of deletion confirmation)
    elif intent_display_name == 'delete.reminder - no': # Assuming this is your Dialogflow intent name
        session_id = req['session']
        # Clear the awaiting_deletion_confirmation context
        response = {
            "fulfillmentText": "Okay, I won't delete that reminder. What else can I help you with?",
            "outputContexts": [
                {
                    "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                    "lifespanCount": 0 # This clears the context
                }
            ]
        }
        print(f"User declined deletion. Context 'awaiting_deletion_confirmation' cleared.")
        return jsonify(response)


    # Handle update.reminder intent (initial request to find and ask for confirmation) <--- NEW BLOCK
    elif intent_display_name == 'update.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task_to_update = parameters.get('task')
        old_date_time_str = parameters.get('old-date-time')
        new_date_time_str = parameters.get('new-date-time')

        if not task_to_update or not old_date_time_str or not new_date_time_str:
            return jsonify({
                "fulfillmentText": "To change a reminder, please tell me the task, its current time, and the new time. E.g., 'change my sleep reminder from 2pm to 4pm'."
            })

        try:
            old_dt_obj = datetime.fromisoformat(old_date_time_str)
            new_dt_obj = datetime.fromisoformat(new_date_time_str) # Keep new_dt_obj for later update

            # Create a small time window around the old time to find the reminder
            time_window_start = old_dt_obj - timedelta(minutes=1)
            time_window_end = old_dt_obj + timedelta(minutes=1)

            query = db.collection('reminders') \
                      .where('task', '==', task_to_update) \
                      .where('status', '==', 'pending') \
                      .where('remind_at', '>=', time_window_start) \
                      .where('remind_at', '<=', time_window_end) \
                      .limit(1)

            docs = query.get()

            if docs:
                reminder_doc = next(iter(docs))
                reminder_id = reminder_doc.id
                reminder_data = reminder_doc.to_dict()

                # Format old and new times for user-friendly display in local timezone
                user_friendly_old_time_str = reminder_data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                user_friendly_new_time_str = new_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")

                session_id = req['session']
                response = {
                    "fulfillmentText": f"I found your reminder to '{task_to_update}' at {user_friendly_old_time_str}. Do you want to change it to {user_friendly_new_time_str}?",
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_update_confirmation",
                            "lifespanCount": 2, # Context active for 2 turns
                            "parameters": {
                                "reminder_id_to_update": reminder_id,
                                "reminder_task_found": task_to_update,
                                "reminder_old_time_found": user_friendly_old_time_str, # Storing formatted time for confirmation
                                "reminder_new_time_desired_str": new_date_time_str # Store ISO string for precise update later
                            }
                        }
                    ]
                }
                print(f"Found reminder {reminder_id} for update. Awaiting confirmation.")
                return jsonify(response)
            else:
                user_friendly_old_time_str = old_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                return jsonify({
                    "fulfillmentText": f"I couldn't find a pending reminder to '{task_to_update}' at {user_friendly_old_time_str}. Please make sure the task and time are correct."
                })

        except ValueError as e:
            print(f"Date parsing error in update: {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding the times. Please use a clear format like 'from 2 PM to 4 PM'."
            })
        except Exception as e:
            print(f"An unexpected error occurred during reminder lookup for update: {e}")
            return jsonify({
                "fulfillmentText": "I'm sorry, something went wrong while trying to find your reminder for update. Please try again later."
            })

    # Handle update.reminder - yes intent (confirmation step) <--- NEW BLOCK
    elif intent_display_name == 'update.reminder - yes':
        reminder_id_to_update = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_id_to_update')
        reminder_task_found = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_task_found')
        reminder_old_time_found = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_old_time_found')
        reminder_new_time_desired_str = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_new_time_desired_str')

        if reminder_id_to_update and reminder_new_time_desired_str:
            try:
                # Convert the stored new time string back to a datetime object for the update
                new_dt_obj_for_update = datetime.fromisoformat(reminder_new_time_desired_str)

                db.collection('reminders').document(reminder_id_to_update).update({
                    'remind_at': new_dt_obj_for_update
                })
                print(f"Reminder (ID: {reminder_id_to_update}) confirmed and updated to {new_dt_obj_for_update}.")

                session_id = req['session']
                response = {
                    "fulfillmentText": f"Okay, I've successfully changed your reminder to '{reminder_task_found}' to {reminder_old_time_found}.", # Use old time for context, new time implied
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_update_confirmation",
                            "lifespanCount": 0 # Clear the context
                        }
                    ]
                }
                return jsonify(response)
            except ValueError as e:
                print(f"Error parsing new time from context: {e}")
                return jsonify({
                    "fulfillmentText": "I had trouble confirming the new time for the reminder. Please try to change it again."
                })
            except Exception as e:
                print(f"Error updating reminder: {e}")
                return jsonify({
                    "fulfillmentText": "I'm sorry, I encountered an error while trying to update the reminder. Please try again."
                })
        else:
            print("Missing reminder ID or new time in context for update confirmation.")
            return jsonify({
                "fulfillmentText": "I'm sorry, I lost track of which reminder you wanted to update. Please tell me again."
            })

    # Handle update.reminder - no intent (negation of update confirmation) <--- NEW BLOCK
    elif intent_display_name == 'update.reminder - no':
        session_id = req['session']
        response = {
            "fulfillmentText": "Okay, I won't change that reminder. What else can I help you with?",
            "outputContexts": [
                {
                    "name": f"{session_id}/contexts/awaiting_update_confirmation",
                    "lifespanCount": 0 # Clear the context
                }
            ]
        }
        print(f"User declined update. Context 'awaiting_update_confirmation' cleared.")
        return jsonify(response)

    return jsonify({
        "fulfillmentText": "I'm not sure how to respond to that yet. Please ask me about setting a reminder!"
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=True)
