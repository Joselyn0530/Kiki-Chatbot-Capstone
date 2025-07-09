import os
import json
import tempfile
from google.cloud import dialogflow
from google.cloud import firestore
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone
import pytz
import openai  # Add this import at the top with other imports

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
# --- END CREDENTIALS SETUP FOR RENDER ---\n
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

        if not task_to_delete: # Task is always required for deletion
            return jsonify({
                "fulfillmentText": "To delete a reminder, please tell me the task, e.g., 'delete my bath reminder'."
            })

        try:
            query = db.collection('reminders') \
                      .where('task', '==', task_to_delete) \
                      .where('status', '==', 'pending') \
                      .order_by('remind_at') # Always order by time

            found_reminders = []

            if date_time_to_delete_str:
                # If a specific time is given, filter by time window
                target_dt_obj = datetime.fromisoformat(date_time_to_delete_str)
                time_window_start = target_dt_obj - timedelta(minutes=1)
                time_window_end = target_dt_obj + timedelta(minutes=1)
                query = query.where('remind_at', '>=', time_window_start) \
                             .where('remind_at', '<=', time_window_end)
                
                # Try to get only one if time is specified, to match exact
                docs = query.limit(1).get() 
                
                # If a specific time was given and found, proceed directly to confirmation
                if docs:
                    reminder_doc = next(iter(docs))
                    reminder_data = reminder_doc.to_dict()
                    reminder_id = reminder_doc.id
                    actual_user_friendly_time_str = reminder_data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    
                    session_id = req['session']
                    response = {
                        "fulfillmentText": f"I found your reminder to '{reminder_data['task']}' at {actual_user_friendly_time_str}. Do you want me to delete it?",
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                                "lifespanCount": 2, 
                                "parameters": {
                                    "reminder_id_to_delete": reminder_id,
                                    "reminder_task_found": reminder_data['task'],
                                    "reminder_time_found": actual_user_friendly_time_str
                                }
                            }
                        ]
                    }
                    print(f"Found specific reminder {reminder_id} for deletion. Awaiting confirmation.")
                    return jsonify(response)
                else:
                    # No specific reminder found with provided time
                    user_friendly_target_time_str = target_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    return jsonify({
                        "fulfillmentText": f"I couldn't find a pending reminder to '{task_to_delete}' around {user_friendly_target_time_str}. Please make sure the task and time are correct and it's still pending."
                    })
            else:
                # No specific time provided, list multiple if found
                docs = query.limit(5).get() # Limit to a reasonable number of results

                for doc in docs:
                    data = doc.to_dict()
                    found_reminders.append({
                        'id': doc.id,
                        'task': data['task'],
                        'remind_at': data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    })

                if len(found_reminders) == 1:
                    # Exactly one reminder found, proceed directly to confirmation
                    reminder = found_reminders[0]
                    session_id = req['session']
                    response = {
                        "fulfillmentText": f"I found your reminder to '{reminder['task']}' at {reminder['remind_at']}. Do you want me to delete it?",
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                                "lifespanCount": 2, 
                                "parameters": {
                                    "reminder_id_to_delete": reminder['id'],
                                    "reminder_task_found": reminder['task'],
                                    "reminder_time_found": reminder['remind_at']
                                }
                            }
                        ]
                    }
                    print(f"Found unique reminder {reminder['id']} for deletion. Awaiting confirmation.")
                    return jsonify(response)

                elif len(found_reminders) > 1:
                    # Multiple reminders found, ask user to clarify
                    reminder_list_text = "I found a few reminders to '{task_to_delete}':\n".format(task_to_delete=task_to_delete)
                    clarification_reminders_data = [] # To store in context

                    for i, reminder in enumerate(found_reminders):
                        reminder_list_text += f"{i+1}. at {reminder['remind_at']}\n"
                        clarification_reminders_data.append({
                            'id': reminder['id'],
                            'task': reminder['task'],
                            'time': reminder['remind_at']
                        })
                    reminder_list_text += "Which one do you want to delete?"

                    session_id = req['session']
                    response = {
                        "fulfillmentText": reminder_list_text,
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_reminder_selection",
                                "lifespanCount": 2, # Active for a couple turns
                                "parameters": {
                                    "reminders_list": json.dumps(clarification_reminders_data), # Store as JSON string
                                    "action_type": "delete" # Indicate the action to perform later
                                }
                            }
                        ]
                    }
                    print(f"Multiple reminders found for '{task_to_delete}'. Asking for clarification.")
                    return jsonify(response)
                
                else:
                    # No reminders found for the task
                    return jsonify({
                        "fulfillmentText": f"I couldn't find any upcoming pending reminder to '{task_to_delete}'. Please make sure the task is correct."
                    })

        except ValueError as e:
            print(f"Date parsing error in delete (should not happen if no date_time_str): {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding your request. Please try again."
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


    # Handle update.reminder intent (initial request to find and ask for confirmation)
    elif intent_display_name == 'update.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task_to_update = parameters.get('task')
        old_date_time_str = parameters.get('old-date-time')
        new_date_time_str = parameters.get('new-date-time')

        if not task_to_update or not new_date_time_str: # old-date-time can be optional for clarification flow
            return jsonify({
                "fulfillmentText": "To change a reminder, please tell me the task and the new time. E.g., 'change my sleep reminder to 4pm' or 'change my sleep reminder from 2pm to 4pm'."
            })

        try:
            # Base query for pending tasks, ordered by time
            query = db.collection('reminders') \
                      .where('task', '==', task_to_update) \
                      .where('status', '==', 'pending') \
                      .order_by('remind_at') 

            found_reminders = []
            
            if old_date_time_str:
                # If a specific old time is given, filter by time window
                old_dt_obj = datetime.fromisoformat(old_date_time_str)
                time_window_start = old_dt_obj - timedelta(minutes=1)
                time_window_end = old_dt_obj + timedelta(minutes=1)
                query = query.where('remind_at', '>=', time_window_start) \
                             .where('remind_at', '<=', time_window_end)
                
                # Try to get only one if old time is specified, to match exact
                docs = query.limit(1).get()

                if docs:
                    reminder_doc = next(iter(docs))
                    reminder_id = reminder_doc.id
                    reminder_data = reminder_doc.to_dict()

                    # Format old and new times for user-friendly display in local timezone
                    user_friendly_old_time_str = reminder_data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    new_dt_obj = datetime.fromisoformat(new_date_time_str)
                    user_friendly_new_time_str = new_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")

                    session_id = req['session']
                    response = {
                        "fulfillmentText": f"I found your reminder to '{task_to_update}' at {user_friendly_old_time_str}. Do you want to change it to {user_friendly_new_time_str}?",
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_update_confirmation",
                                "lifespanCount": 2, 
                                "parameters": {
                                    "reminder_id_to_update": reminder_id,
                                    "reminder_task_found": task_to_update,
                                    "reminder_old_time_found": user_friendly_old_time_str, 
                                    "reminder_new_time_desired_iso_str": new_date_time_str,
                                    "reminder_new_time_desired_formatted": user_friendly_new_time_str
                                }
                            }
                        ]
                    }
                    print(f"Found specific reminder {reminder_id} for update. Awaiting confirmation.")
                    return jsonify(response)
                else:
                    # No specific reminder found with provided old time
                    old_dt_obj = datetime.fromisoformat(old_date_time_str)
                    user_friendly_old_time_str = old_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    return jsonify({
                        "fulfillmentText": f"I couldn't find a pending reminder to '{task_to_update}' at {user_friendly_old_time_str}. Please make sure the task and current time are correct."
                    })
            else:
                # No specific old time provided, list multiple if found
                docs = query.limit(5).get() # Limit to a reasonable number of results

                for doc in docs:
                    data = doc.to_dict()
                    found_reminders.append({
                        'id': doc.id,
                        'task': data['task'],
                        'remind_at': data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    })

                if len(found_reminders) == 1:
                    # Exactly one reminder found, proceed directly to confirmation
                    reminder = found_reminders[0]
                    session_id = req['session']
                    
                    new_dt_obj = datetime.fromisoformat(new_date_time_str)
                    user_friendly_new_time_str = new_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")

                    response = {
                        "fulfillmentText": f"I found your reminder to '{reminder['task']}' at {reminder['remind_at']}. Do you want to change it to {user_friendly_new_time_str}?",
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_update_confirmation",
                                "lifespanCount": 2, 
                                "parameters": {
                                    "reminder_id_to_update": reminder['id'],
                                    "reminder_task_found": reminder['task'],
                                    "reminder_old_time_found": reminder['remind_at'], 
                                    "reminder_new_time_desired_iso_str": new_date_time_str,
                                    "reminder_new_time_desired_formatted": user_friendly_new_time_str
                                }
                            }
                        ]
                    }
                    print(f"Found unique reminder {reminder['id']} for update. Awaiting confirmation.")
                    return jsonify(response)

                elif len(found_reminders) > 1:
                    # Multiple reminders found, ask user to clarify
                    reminder_list_text = "I found a few reminders to '{task_to_update}':\n".format(task_to_update=task_to_update)
                    clarification_reminders_data = [] # To store in context

                    for i, reminder in enumerate(found_reminders):
                        reminder_list_text += f"{i+1}. at {reminder['remind_at']}\n"
                        clarification_reminders_data.append({
                            'id': reminder['id'],
                            'task': reminder['task'],
                            'time': reminder['remind_at']
                        })
                    reminder_list_text += "Which one do you want to change?"

                    session_id = req['session']
                    response = {
                        "fulfillmentText": reminder_list_text,
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_reminder_selection",
                                "lifespanCount": 2, # Active for a couple turns
                                "parameters": {
                                    "reminders_list": json.dumps(clarification_reminders_data), # Store as JSON string
                                    "action_type": "update", # Indicate the action to perform later
                                    "new_time_iso_str_for_update": new_date_time_str # Store the new time for the update action
                                }
                            }
                        ]
                    }
                    print(f"Multiple reminders found for '{task_to_update}'. Asking for clarification for update.")
                    return jsonify(response)
                
                else:
                    # No reminders found for the task
                    return jsonify({
                        "fulfillmentText": f"I couldn't find any upcoming pending reminder to '{task_to_update}'. Please make sure the task is correct."
                    })

        except ValueError as e:
            print(f"Date parsing error in update (should not happen if no old_date_time_str): {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding your request. Please try again."
            })
        except Exception as e:
            print(f"An unexpected error occurred during reminder lookup for update: {e}")
            return jsonify({
                "fulfillmentText": "I'm sorry, something went wrong while trying to find your reminder for update. Please try again later."
            })

    # Handle update.reminder - yes intent (confirmation step)
    elif intent_display_name == 'update.reminder - yes':
        reminder_id_to_update = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_id_to_update')
        reminder_task_found = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_task_found')
        reminder_new_time_desired_iso_str = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_new_time_desired_iso_str')
        reminder_new_time_desired_formatted = get_context_parameter(req, 'awaiting_update_confirmation', 'reminder_new_time_desired_formatted')

        if reminder_id_to_update and reminder_new_time_desired_iso_str:
            try:
                new_dt_obj_for_update = datetime.fromisoformat(reminder_new_time_desired_iso_str)

                db.collection('reminders').document(reminder_id_to_update).update({
                    'remind_at': new_dt_obj_for_update
                })
                print(f"Reminder (ID: {reminder_id_to_update}) confirmed and updated to {new_dt_obj_for_update}.")

                session_id = req['session']
                response = {
                    "fulfillmentText": f"Okay, I've successfully changed your reminder to '{reminder_task_found}' to {reminder_new_time_desired_formatted}.",
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

    # Handle update.reminder - no intent (negation of update confirmation)
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

    # Handle select.reminder_to_manage intent (user clarifies which reminder from a list)
    elif intent_display_name == 'select.reminder_to_manage':
        parameters = req.get('queryResult', {}).get('parameters', {})
        selection_index = parameters.get('selection_index') # e.g., 1, 2, 3
        selection_time_str = parameters.get('selection_time') # e.g., "6pm", "tomorrow"

        session_id = req['session']
        awaiting_selection_context = None
        for context in req.get('queryResult', {}).get('inputContexts', []):
            if 'awaiting_reminder_selection' in context.get('name', ''):
                awaiting_selection_context = context
                break
        
        if not awaiting_selection_context:
            return jsonify({
                "fulfillmentText": "I'm sorry, I'm not sure which list of reminders you're referring to. Please try again from the beginning."
            })

        reminders_list_json = get_context_parameter(req, 'awaiting_reminder_selection', 'reminders_list')
        action_type = get_context_parameter(req, 'awaiting_reminder_selection', 'action_type')
        new_time_iso_str_for_update = get_context_parameter(req, 'awaiting_reminder_selection', 'new_time_iso_str_for_update') # Only present for update action

        if not reminders_list_json:
            return jsonify({
                "fulfillmentText": "I'm sorry, I don't have the list of reminders anymore. Could you please rephrase your request from the beginning?"
            })
        
        reminders_list = json.loads(reminders_list_json)
        selected_reminder = None

        if selection_index is not None and selection_index > 0 and selection_index <= len(reminders_list):
            selected_reminder = reminders_list[selection_index - 1] # -1 because list is 0-indexed
            print(f"Selected reminder by index: {selected_reminder}")
        elif selection_time_str:
            # Try to match by time (e.g., "the one at 6pm")
            try:
                selected_dt = datetime.fromisoformat(selection_time_str).astimezone(KUALA_LUMPUR_TZ)
                
                # Iterate through reminders_list, parse their time strings back to datetime objects for comparison
                for reminder in reminders_list:
                    # Parse the string time from context back to datetime object
                    reminder_time_dt = datetime.strptime(reminder['time'], "%I:%M %p on %B %d, %Y").astimezone(KUALA_LUMPUR_TZ)
                    
                    # Compare only hour and minute, and potentially date if specified in selection_time_str
                    # For simplicity, let's compare exact time including date for now.
                    # More robust comparison needed for "today", "tomorrow" etc.
                    
                    # Check if the minute difference is small enough (e.g., within 1 minute)
                    time_difference = abs((selected_dt - reminder_time_dt).total_seconds())
                    if time_difference < 60: # If difference is less than 60 seconds
                        selected_reminder = reminder
                        print(f"Selected reminder by time: {selected_reminder}")
                        break
            except ValueError as e:
                print(f"Error parsing selection_time_str: {e}")
                # Handle cases where selection_time_str is not a precise datetime (e.g., "today", "tomorrow")
                # This would require more sophisticated date/time matching.
                pass 
        
        if selected_reminder:
            # Now, based on action_type, transition to the correct confirmation flow
            if action_type == "delete":
                response = {
                    "fulfillmentText": f"You want to delete the reminder to '{selected_reminder['task']}' at {selected_reminder['time']}. Confirm delete?",
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_reminder_selection",
                            "lifespanCount": 0 # Clear the selection context
                        },
                        {
                            "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                            "lifespanCount": 2, 
                            "parameters": {
                                "reminder_id_to_delete": selected_reminder['id'],
                                "reminder_task_found": selected_reminder['task'],
                                "reminder_time_found": selected_reminder['time']
                            }
                        }
                    ]
                }
                print("Transitioning to deletion confirmation.")
                return jsonify(response)
            elif action_type == "update" and new_time_iso_str_for_update:
                # Need to convert new_time_iso_str_for_update for formatted display
                new_dt_obj_for_display = datetime.fromisoformat(new_time_iso_str_for_update)
                new_time_formatted = new_dt_obj_for_display.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")

                response = {
                    "fulfillmentText": f"You want to change the reminder to '{selected_reminder['task']}' at {selected_reminder['time']} to {new_time_formatted}. Confirm update?",
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_reminder_selection",
                            "lifespanCount": 0 # Clear the selection context
                        },
                        {
                            "name": f"{session_id}/contexts/awaiting_update_confirmation",
                            "lifespanCount": 2, 
                            "parameters": {
                                "reminder_id_to_update": selected_reminder['id'],
                                "reminder_task_found": selected_reminder['task'],
                                "reminder_old_time_found": selected_reminder['time'], # Store selected reminder's old time
                                "reminder_new_time_desired_iso_str": new_time_iso_str_for_update,
                                "reminder_new_time_desired_formatted": new_time_formatted
                            }
                        }
                    ]
                }
                print("Transitioning to update confirmation.")
                return jsonify(response)
            else:
                return jsonify({
                    "fulfillmentText": "I'm sorry, I couldn't determine the action you want to perform for the selected reminder."
                })
        else:
            return jsonify({
                "fulfillmentText": "I couldn't identify which reminder you meant. Please choose a number from the list or try specifying the time more precisely."
            })
    
    # Add OpenAI GPT-3.5-Turbo integration for FeelingHappyIntent
    elif intent_display_name == 'Feelinghappyintent':
        user_message = req.get('queryResult', {}).get('queryText', '')
        openai_api_key = os.environ.get('OPENAI_API_KEY')
        if not openai_api_key:
            return jsonify({
                "fulfillmentText": "OpenAI API key is not set on the server."
            })
        try:
            client = openai.OpenAI(api_key=openai_api_key)
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a warm, friendly, and supportive companion chatbot for elderly users. Always provide emotional support, empathy, and encouragement. Respond in a gentle, caring, and positive manner, suitable for older adults who may be feeling lonely or in need of a friend."},
                    {"role": "user", "content": user_message}
                ]
            )
            ai_reply = response.choices[0].message.content
            return jsonify({
                "fulfillmentText": ai_reply
            })
        except Exception as e:
            print(f"OpenAI API error: {e}")
            return jsonify({
                "fulfillmentText": "Sorry, I couldn't process your request right now."
            })
    
    # Fallback if no specific intent is matched
    return jsonify({
        "fulfillmentText": "I'm not sure how to respond to that yet. Please ask me about setting, deleting, or updating a reminder!"
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=True)
