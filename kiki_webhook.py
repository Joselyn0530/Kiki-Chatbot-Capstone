import os
import json
import tempfile
from google.cloud import dialogflow
from google.cloud import firestore
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone
import pytz
import openai  # Add this import at the top with other imports
from dateutil import parser

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

# Helper to extract date-time string from dict or return as-is
def extract_datetime_str(dt):
    if isinstance(dt, dict) and 'date_time' in dt:
        return str(dt['date_time'])
    return str(dt) if dt is not None else ''

# Helper to clear all update-related contexts
def clear_all_update_contexts(session_id):
    return [
        {"name": f"{session_id}/contexts/awaiting_deletion_confirmation", "lifespanCount": 0},
        {"name": f"{session_id}/contexts/awaiting_update_confirmation", "lifespanCount": 0},
        {"name": f"{session_id}/contexts/awaiting_update_time", "lifespanCount": 0},
        {"name": f"{session_id}/contexts/awaiting_update_selection", "lifespanCount": 0},
        {"name": f"{session_id}/contexts/awaiting_deletion_selection", "lifespanCount": 0}
    ]

@app.route('/', methods=['POST'])
def webhook():
    req = request.get_json(silent=True, force=True)
    print(f"Dialogflow Request: {req}")

    # Debug: Print all input and output contexts for every request
    input_contexts = req.get('queryResult', {}).get('inputContexts', [])
    output_contexts = req.get('queryResult', {}).get('outputContexts', [])
    print("Input Contexts:", input_contexts)
    print("Output Contexts:", output_contexts)

    intent_display_name = req.get('queryResult', {}).get('intent', {}).get('displayName')
    print(f"Intent Display Name: {intent_display_name}")

    if intent_display_name == 'set.reminder':
        # Clear any lingering update contexts when starting a new reminder flow
        session_id = req['session']
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')
        if isinstance(task, list):
            task = task[0] if task else None
        if task:
            task = task.strip().lower()
        GENERIC_TASKS = {"set a reminder", "reminder", "remind me", "remind", "add reminder"}
        if task and task in GENERIC_TASKS:
            # Clear both task and time contexts to avoid using previous values
            return jsonify({
                "fulfillmentText": "Sure! ☺️ What should I remind you about?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_task",
                        "lifespanCount": 2,
                        "parameters": {}
                    },
                    {
                        "name": f"{req['session']}/contexts/await_time",
                        "lifespanCount": 0,
                        "parameters": {}
                    }
                ]
            })
        date_time_str = parameters.get('date-time')

        # Improved missing parameter handling
        if not task and not date_time_str:
            return jsonify({
                "fulfillmentText": "Sure! 😊 What should I remind you about?"
            })
        elif not task and date_time_str:
            # Format the time for user-friendly display
            try:
                dt_str = extract_datetime_str(date_time_str)
                dt_obj = datetime.fromisoformat(str(dt_str))
                user_friendly_time_str = dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
            except Exception:
                user_friendly_time_str = str(date_time_str)
            # Save time to context and ask for task
            return jsonify({
                "fulfillmentText": f"Okay! 🕒 I got the time: {user_friendly_time_str}. What should I remind you about?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_task",
                        "lifespanCount": 2,
                        "parameters": {
                            "date-time": date_time_str
                        }
                    }
                ]
            })
        elif not date_time_str:
            # Save task to context and ask for time
            return jsonify({
                "fulfillmentText": f"Got it — you want me to remind you to {task}. 📝 When should I remind you?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_time",
                        "lifespanCount": 2,
                        "parameters": {
                            "task": task
                        }
                    }
                ]
            })
        else:
            try:
                dt_str = extract_datetime_str(date_time_str)
                reminder_dt_obj = datetime.fromisoformat(str(dt_str))

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
                        {"text": {"text": [f"Got it! I'll remind you to {task} at {user_friendly_time_str}."]}
                        }
                    ],
                    "outputContexts": [
                        {
                            "name": f"{req['session']}/contexts/await_task",
                            "lifespanCount": 0,
                            "parameters": {}
                        },
                        {
                            "name": f"{req['session']}/contexts/await_time",
                            "lifespanCount": 0,
                            "parameters": {}
                        }
                    ] + clear_all_update_contexts(session_id)
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
        # Clear any lingering update contexts when starting a delete flow
        session_id = req['session']
        parameters = req.get('queryResult', {}).get('parameters', {})
        task_to_delete = parameters.get('task')
        if task_to_delete:
            task_to_delete = task_to_delete.strip().lower()
        date_time_to_delete_str = parameters.get('date-time')

        if not task_to_delete:
            return jsonify({
                "fulfillmentText": "To delete a reminder, please tell me the task, e.g., 'delete bath reminder'."
            })

        try:
            query = db.collection('reminders') \
                      .where('task', '==', task_to_delete) \
                      .where('status', '==', 'pending') \
                      .order_by('remind_at')

            found_reminders = []

            if date_time_to_delete_str:
                # If a specific time is given, filter by time window
                try:
                    target_dt_obj = parser.isoparse(date_time_to_delete_str)
                except Exception:
                    return jsonify({
                        "fulfillmentText": "Sorry, I couldn't understand the time you gave. Please use a clear format like 'tomorrow at 2 PM'."
                    })
                time_window_start = target_dt_obj - timedelta(minutes=1)
                time_window_end = target_dt_obj + timedelta(minutes=1)
                query = query.where('remind_at', '>=', time_window_start) \
                             .where('remind_at', '<=', time_window_end)
                docs = query.limit(1).get()
                if docs:
                    reminder_doc = next(iter(docs))
                    reminder_data = reminder_doc.to_dict()
                    reminder_id = reminder_doc.id
                    actual_user_friendly_time_str = reminder_data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    actual_iso_time_str = reminder_data['remind_at'].isoformat()
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
                                    "reminder_time_found_str": actual_user_friendly_time_str,
                                    "reminder_time_found_raw": actual_iso_time_str
                                }
                            }
                        ]
                    }
                    return jsonify(response)
                else:
                    user_friendly_target_time_str = target_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    return jsonify({
                        "fulfillmentText": f"I couldn't find a pending reminder to '{task_to_delete}' around {user_friendly_target_time_str}. Please make sure the task and time are correct and it's still pending."
                    })
            else:
                docs = query.limit(5).get()
                for doc in docs:
                    data = doc.to_dict()
                    found_reminders.append({
                        'id': doc.id,
                        'task': data['task'],
                        'remind_at': data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y"),
                        'remind_at_raw': data['remind_at'].isoformat()
                    })
                if len(found_reminders) == 1:
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
                                    "reminder_time_found_str": reminder['remind_at'],
                                    "reminder_time_found_raw": reminder['remind_at_raw']
                                }
                            }
                        ]
                    }
                    return jsonify(response)
                elif len(found_reminders) > 1:
                    clarification_reminders_data = []
                    rich_content_items = []
                    for i, reminder in enumerate(found_reminders):
                        rich_content_items.append({
                            "type": "info",
                            "title": f"{i+1}. {reminder['task'].capitalize()}",
                            "subtitle": f"at {reminder['remind_at']}",
                            "event": {
                                "name": "",
                                "languageCode": "",
                                "parameters": {}
                            }
                        })
                        clarification_reminders_data.append({
                            'id': reminder['id'],
                            'task': reminder['task'],
                            'time': reminder['remind_at'],
                            'time_raw': reminder['remind_at_raw']
                        })
                    # Add prompt as a description card
                    rich_content_items.append({
                        "type": "description",
                        "text": ["Please reply with the number, like “1” or “2”."]
                    })
                    session_id = req['session']
                    response = {
                        "fulfillmentMessages": [
                            {
                                "payload": {
                                    "richContent": [rich_content_items]
                                }
                            }
                        ],
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_deletion_selection",
                                "lifespanCount": 2,
                                "parameters": {
                                    "reminders_list": json.dumps(clarification_reminders_data),
                                    "action_type": "delete"
                                }
                            }
                        ]
                    }
                    return jsonify(response)
                else:
                    return jsonify({
                        "fulfillmentText": f"I couldn't find any upcoming pending reminder to '{task_to_delete}'. Please make sure the task is correct."
                    })
        except Exception as e:
            print(f"An unexpected error occurred during reminder lookup for deletion: {e}")
            return jsonify({
                "fulfillmentText": "I'm sorry, something went wrong while trying to find your reminder for deletion. Please try again later."
            })

    # Handle selection by index for deletion
    elif intent_display_name == 'select.reminder_to_manage_delete':
        parameters = req.get('queryResult', {}).get('parameters', {})
        selection_index = parameters.get('selection_index')
        session_id = req['session']
        # Get reminders list from context
        reminders_list_json = None
        for context in req.get('queryResult', {}).get('outputContexts', []):
            if 'awaiting_deletion_selection' in context.get('name', ''):
                reminders_list_json = context.get('parameters', {}).get('reminders_list')
                break
        if not reminders_list_json:
            return jsonify({
                "fulfillmentText": "I'm sorry, I don't have the list of reminders anymore. Could you please rephrase your request from the beginning?"
            })
        reminders_list = json.loads(reminders_list_json)
        # Safety check for selection_index
        try:
            selection_index = int(selection_index)
        except (ValueError, TypeError):
            return jsonify({
                "fulfillmentText": "I didn’t catch which one you want to delete. Please reply with a number like 1 or 2."
            })
        if selection_index is not None and 1 <= selection_index <= len(reminders_list):
            selected_reminder = reminders_list[selection_index - 1]
            response = {
                "fulfillmentText": f"You want to delete the reminder to '{selected_reminder['task']}' at {selected_reminder['time']}. Confirm delete it?",
                "outputContexts": [
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
            return jsonify(response)
        else:
            return jsonify({
                "fulfillmentText": "I couldn't identify which reminder you meant. Please choose a number from the list or try specifying the time more precisely."
            })

    # Handle delete.reminder - yes intent (confirmation step)
    elif intent_display_name == 'delete.reminder - yes':
        reminder_id_to_delete = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_id_to_delete')
        reminder_task_found = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_task_found')
        reminder_time_found_str = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_time_found_str')
        reminder_time_found_raw = get_context_parameter(req, 'awaiting_deletion_confirmation', 'reminder_time_found_raw')
        session_id = req['session']
        if reminder_id_to_delete:
            try:
                db.collection('reminders').document(reminder_id_to_delete).delete()
                return jsonify({
                    "fulfillmentText": f"Your reminder to '{reminder_task_found}' at {reminder_time_found_str} has been deleted.",
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                            "lifespanCount": 0
                        },
                        {
                            "name": f"{session_id}/contexts/awaiting_deletion_selection",
                            "lifespanCount": 0
                        }
                    ] + clear_all_update_contexts(session_id)
                })
            except Exception as e:
                print(f"Error deleting reminder from context: {e}")
                return jsonify({
                    "fulfillmentText": "Sorry, I couldn't delete your reminder. Please try again."
                })
        else:
            return jsonify({
                "fulfillmentText": "I'm sorry, I lost track of which reminder you wanted to delete. Please tell me again."
            })

    # Handle delete.reminder - no intent (negation of deletion confirmation)
    elif intent_display_name == 'delete.reminder - no':
        session_id = req['session']
        return jsonify({
            "fulfillmentText": "Okay, I won't delete that reminder. What else can I help you with?",
            "outputContexts": [
                {
                    "name": f"{session_id}/contexts/awaiting_deletion_confirmation",
                    "lifespanCount": 0
                },
                {
                    "name": f"{session_id}/contexts/awaiting_deletion_selection",
                    "lifespanCount": 0
                }
            ] + clear_all_update_contexts(session_id)
        })

    # Handle update.reminder intent (initial request to find and ask for confirmation)
    elif intent_display_name == 'update.reminder':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task_to_update = parameters.get('task')
        old_date_time_str = parameters.get('old-date-time')
        new_date_time_str = parameters.get('new-date-time')

        # Check for generic task names
        GENERIC_TASK_KEYWORDS = {"reminder", "reminders", "the reminder", "the reminders", "my reminder", "my reminders"}
        if task_to_update and task_to_update.lower() in GENERIC_TASK_KEYWORDS:
            session_id = req['session']
            return jsonify({
                "fulfillmentText": "Which reminder do you mean? You can say things like 'change my bath reminder' or 'change my sleep reminder'.",
                "outputContexts": clear_all_update_contexts(session_id)
            })
        
        if not task_to_update:
            # No task provided, but check if we have a time to search by
            if old_date_time_str:
                try:
                    # Search for all reminders around the specified time
                    old_dt_obj = datetime.fromisoformat(old_date_time_str)
                    time_window_start = old_dt_obj - timedelta(minutes=1)
                    time_window_end = old_dt_obj + timedelta(minutes=1)
                    
                    query = db.collection('reminders') \
                              .where('status', '==', 'pending') \
                              .where('remind_at', '>=', time_window_start) \
                              .where('remind_at', '<=', time_window_end) \
                              .order_by('remind_at')
                    
                    docs = query.limit(5).get()
                    found_reminders = []
                    
                    for doc in docs:
                        data = doc.to_dict()
                        found_reminders.append({
                            'id': doc.id,
                            'task': data['task'],
                            'remind_at': data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                        })
                    
                    if len(found_reminders) == 0:
                        user_friendly_time_str = old_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                        return jsonify({
                            "fulfillmentText": f"I couldn't find any pending reminders at {user_friendly_time_str}. Please make sure the time is correct."
                        })
                    elif len(found_reminders) == 1:
                        # Only one reminder found at that time
                        reminder = found_reminders[0]
                        session_id = req['session']
                        
                        if new_date_time_str:
                            # New time provided, proceed to confirmation
                            try:
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
                                return jsonify(response)
                            except ValueError as e:
                                print(f"Error parsing new time: {e}")
                                return jsonify({
                                    "fulfillmentText": f"I couldn't understand the new time '{new_date_time_str}'. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
                                })
                        else:
                            # No new time provided, ask for the new time
                            response = {
                                "fulfillmentText": f"I found your reminder to '{reminder['task']}' at {reminder['remind_at']}. Sure. What's the new time for your reminder?",
                                "outputContexts": [
                                    {
                                        "name": f"{session_id}/contexts/awaiting_update_time",
                                        "lifespanCount": 2,
                                        "parameters": {
                                            "reminder_id_to_update": reminder['id'],
                                            "reminder_task_found": reminder['task'],
                                            "reminder_old_time_found": reminder['remind_at']
                                        }
                                    }
                                ]
                            }
                            return jsonify(response)
                    else:
                        # Multiple reminders found at that time
                        user_friendly_time_str = old_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                        reminder_list_text = f"I found a few reminders at {user_friendly_time_str}:\n\n"
                        clarification_reminders_data = []

                        for i, reminder in enumerate(found_reminders):
                            reminder_list_text += f"- {reminder['task']} reminder\n"
                            clarification_reminders_data.append({
                                'id': reminder['id'],
                                'task': reminder['task'],
                                'time': reminder['remind_at'],
                                'time_raw': reminder['remind_at']
                            })
                        reminder_list_text += "\nWhich reminder would you like to change?"

                        session_id = req['session']
                        response = {
                            "fulfillmentText": reminder_list_text,
                            "outputContexts": [
                                {
                                    "name": f"{session_id}/contexts/awaiting_update_selection",
                                    "lifespanCount": 2,
                                    "parameters": {
                                        "reminders_list": json.dumps(clarification_reminders_data),
                                        "action_type": "update_no_time" # New action type
                                    }
                                }
                            ]
                        }
                        return jsonify(response)
                        
                except ValueError as e:
                    print(f"Error parsing time: {e}")
                    return jsonify({
                        "fulfillmentText": "I had trouble understanding the time. Please use a clear format like '4pm' or '2 PM'."
                    })
            else:
                # No task and no time provided
                session_id = req['session']
                return jsonify({
                    "fulfillmentText": "To change a reminder, please tell me the task. E.g., 'change sleep reminder' or 'change bath reminder'.",
                    "outputContexts": clear_all_update_contexts(session_id)
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
                try:
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
                        try:
                            new_dt_obj = datetime.fromisoformat(new_date_time_str)
                            user_friendly_new_time_str = new_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                        except ValueError as e:
                            print(f"Error parsing new time: {e}")
                            return jsonify({
                                "fulfillmentText": f"I couldn't understand the new time '{new_date_time_str}'. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
                            })

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
                except ValueError as e:
                    print(f"Error parsing old date time: {e}")
                    return jsonify({
                        "fulfillmentText": "I had trouble understanding the current time. Please use a clear format like '4pm' or '2 PM'."
                    })
            else:
                # No specific old time provided, get all reminders for the task
                docs = query.limit(5).get() # Limit to a reasonable number of results

                for doc in docs:
                    data = doc.to_dict()
                    found_reminders.append({
                        'id': doc.id,
                        'task': data['task'],
                        'remind_at': data['remind_at'].astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                    })

            if len(found_reminders) == 0:
                # No reminders found for the task
                return jsonify({
                    "fulfillmentText": f"I couldn't find any upcoming pending reminder to '{task_to_update}'. Please make sure the task is correct."
                })
            
            elif len(found_reminders) == 1:
                # Exactly one reminder found
                reminder = found_reminders[0]
                session_id = req['session']
                
                if new_date_time_str:
                    # New time provided, proceed to confirmation
                    try:
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
                    except ValueError as e:
                        print(f"Error parsing new time: {e}")
                        return jsonify({
                            "fulfillmentText": f"I couldn't understand the new time '{new_date_time_str}'. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
                        })
                else:
                    # No new time provided, ask for the new time
                    response = {
                        "fulfillmentText": f"I found your reminder to '{reminder['task']}' at {reminder['remind_at']}. Sure. What's the new time for your reminder?",
                        "outputContexts": [
                            {
                                "name": f"{session_id}/contexts/awaiting_update_time",
                                "lifespanCount": 2,
                                "parameters": {
                                    "reminder_id_to_update": reminder['id'],
                                    "reminder_task_found": reminder['task'],
                                    "reminder_old_time_found": reminder['remind_at']
                                }
                            }
                        ]
                    }
                    print(f"Found unique reminder {reminder['id']} for update. Asking for new time.")
                    return jsonify(response)

            elif len(found_reminders) > 1:
                # Multiple reminders found, show rich content for selection
                clarification_reminders_data = []
                rich_content_items = []

                for i, reminder in enumerate(found_reminders):
                    rich_content_items.append({
                        "type": "info",
                        "title": f"{i+1}. {reminder['task'].capitalize()}",
                        "subtitle": f"at {reminder['remind_at']}"
                    })
                    clarification_reminders_data.append({
                        'id': reminder['id'],
                        'task': reminder['task'],
                        'time': reminder['remind_at'],
                        'time_raw': reminder['remind_at']
                    })

                # Add prompt as a description card
                rich_content_items.append({
                    "type": "description",
                    "text": [
                        "Which one would you like to change? Please reply with a number, like '1' or '2'."
                    ]
                })

                session_id = req['session']
                response = {
                    "fulfillmentMessages": [
                        {
                            "payload": {
                                "richContent": [rich_content_items]
                            }
                        }
                    ],
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/awaiting_update_selection",
                            "lifespanCount": 2,
                            "parameters": {
                                "reminders_list": json.dumps(clarification_reminders_data),
                                "action_type": "update_no_time" # New action type
                            }
                        }
                    ]
                }
                return jsonify(response)

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
                
                doc_ref = db.collection('reminders').document(reminder_id_to_update)
                doc = doc_ref.get()

                if doc.exists:
                    doc_ref.update({
                        'remind_at': new_dt_obj_for_update
                    })
                    print(f"Reminder (ID: {reminder_id_to_update}) confirmed and updated to {new_dt_obj_for_update}.")

                    session_id = req['session']
                    response = {
                        "fulfillmentText": f"Okay, I've successfully changed your reminder to '{reminder_task_found}' to {reminder_new_time_desired_formatted}.",
                        "outputContexts": clear_all_update_contexts(session_id)
                    }
                    return jsonify(response)
                else:
                    print(f"Attempted to update reminder {reminder_id_to_update} but it doesn't exist.")
                    return jsonify({
                        "fulfillmentText": "I'm sorry, I couldn't find that specific reminder to update. Please try again."
                    })

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
            "outputContexts": clear_all_update_contexts(session_id)
        }
        print(f"User declined update. All update contexts cleared.")
        return jsonify(response)

    # Handle update.reminder_new_time for capturing new time for update
    elif intent_display_name == 'update.reminder_new_time':
        parameters = req.get('queryResult', {}).get('parameters', {})
        new_date_time = parameters.get('date-time')
        
        # Get reminder details from context
        reminder_id_to_update = get_context_parameter(req, 'awaiting_update_time', 'reminder_id_to_update')
        reminder_task_found = get_context_parameter(req, 'awaiting_update_time', 'reminder_task_found')
        reminder_old_time_found = get_context_parameter(req, 'awaiting_update_time', 'reminder_old_time_found')
        
        if not reminder_id_to_update or not new_date_time:
            return jsonify({
                "fulfillmentText": "I'm sorry, I lost track of which reminder you wanted to update. Please try again."
            })
        
        try:
            # Extract and parse the new time
            new_date_time_str = extract_datetime_str(new_date_time)
            new_dt_obj = datetime.fromisoformat(new_date_time_str) # Removed redundant str() cast
            user_friendly_new_time_str = new_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
            
            session_id = req['session']
            response = {
                "fulfillmentText": f"Just to confirm: You want to change your '{reminder_task_found}' reminder from {reminder_old_time_found} to {user_friendly_new_time_str}. Should I go ahead?",
                "outputContexts": [
                    {
                        "name": f"{session_id}/contexts/awaiting_update_time",
                        "lifespanCount": 0 # Clear the time context
                    },
                    {
                        "name": f"{session_id}/contexts/awaiting_update_confirmation",
                        "lifespanCount": 2,
                        "parameters": {
                            "reminder_id_to_update": reminder_id_to_update,
                            "reminder_task_found": reminder_task_found,
                            "reminder_old_time_found": reminder_old_time_found,
                            "reminder_new_time_desired_iso_str": new_date_time_str,
                            "reminder_new_time_desired_formatted": user_friendly_new_time_str
                        }
                    }
                ]
            }
            return jsonify(response)
            
        except ValueError as e:
            print(f"Error parsing new time: {e}")
            return jsonify({
                "fulfillmentText": f"I couldn't understand the time '{new_date_time}'. Please try saying it like 'at 5pm today' or 'tomorrow at 8am'."
            })
        except Exception as e:
            print(f"Error processing update time: {e}")
            return jsonify({
                "fulfillmentText": "I had trouble understanding the time. Please use a clear format like '5pm' or 'tomorrow at 2 PM'."
            })

    # Handle select.reminder_to_manage_update intent (user clarifies which reminder from a list)
    elif intent_display_name == 'select.reminder_to_manage_update':
        print("[DEBUG] Entered select.reminder_to_manage_update handler")
        parameters = req.get('queryResult', {}).get('parameters', {})
        selection_index = parameters.get('selection_index') # e.g., 1, 2, 3
        selection_time_str = parameters.get('selection_time') # e.g., "6pm", "tomorrow"

        session_id = req['session']
        awaiting_selection_context = None
        context_name = None
        # Debug: Print inputContexts again for this handler
        print("[DEBUG] inputContexts in select.reminder_to_manage_update:", req.get('queryResult', {}).get('inputContexts', []))
        
        # Check for both deletion and update selection contexts
        for context in req.get('queryResult', {}).get('inputContexts', []):
            if 'awaiting_deletion_selection' in context.get('name', ''):
                awaiting_selection_context = context
                context_name = 'awaiting_deletion_selection'
                break
            elif 'awaiting_update_selection' in context.get('name', ''):
                awaiting_selection_context = context
                context_name = 'awaiting_update_selection'
                break
        
        if not awaiting_selection_context:
            return jsonify({
                "fulfillmentText": "I'm sorry, I'm not sure which list of reminders you're referring to. Please try again from the beginning."
            })

        reminders_list_json = get_context_parameter(req, context_name, 'reminders_list')
        action_type = get_context_parameter(req, context_name, 'action_type')
        # new_time_iso_str_for_update is only present for update action if it was given initially
        new_time_iso_str_for_update = get_context_parameter(req, context_name, 'new_time_iso_str_for_update') 

        if not reminders_list_json:
            # Fallback: Offer to show the list again
            return jsonify({
                "fulfillmentText": "I lost track of the reminders. Would you like to see the list again?"
            })
        reminders_list = json.loads(reminders_list_json)
        selected_reminder = None

        if selection_index is not None and selection_index > 0 and selection_index <= len(reminders_list):
            selected_reminder = reminders_list[selection_index - 1] # -1 because list is 0-indexed
            print(f"Selected reminder by index: {selected_reminder}")
        elif selection_time_str:
            # Try to match by time (e.g., "the one at 6pm")
            try:
                # Ensure selection_time_str is parsed correctly for comparison
                parsed_selection_dt = extract_datetime_str(selection_time_str)
                selected_dt = datetime.fromisoformat(parsed_selection_dt).astimezone(KUALA_LUMPUR_TZ)
                
                for reminder in reminders_list:
                    # Parse the string time from context back to datetime object for comparison
                    # Using datetime.strptime based on the known format "%I:%M %p on %B %d, %Y"
                    reminder_time_dt = datetime.strptime(reminder['time'], "%I:%M %p on %B %d, %Y").astimezone(KUALA_LUMPUR_TZ)
                    
                    # Compare only minute difference, good for near matches
                    time_difference = abs((selected_dt - reminder_time_dt).total_seconds())
                    if time_difference < 60: # If difference is less than 60 seconds
                        selected_reminder = reminder
                        print(f"Selected reminder by time: {selected_reminder}")
                        break
            except ValueError as e:
                print(f"Error parsing selection_time_str for comparison: {e}")
                pass 
        
        if selected_reminder:
            # Now, based on action_type, transition to the correct confirmation flow
            if action_type == "delete":
                response = {
                    "fulfillmentText": f"You want to delete the reminder to '{selected_reminder['task']}' at {selected_reminder['time']}. Confirm delete?",
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/{context_name}",
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
                            "name": f"{session_id}/contexts/{context_name}",
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
            elif action_type == "update_no_time": # FIX: Handle the new action_type
                response = {
                    "fulfillmentText": f"You've selected the '{selected_reminder['task']}' reminder. What's the new time you'd like to set it to?",
                    "outputContexts": [
                        {
                            "name": f"{session_id}/contexts/{context_name}",
                            "lifespanCount": 0 # Clear the selection context
                        },
                        {
                            "name": f"{session_id}/contexts/awaiting_update_time",
                            "lifespanCount": 2, 
                            "parameters": {
                                "reminder_id_to_update": selected_reminder['id'],
                                "reminder_task_found": selected_reminder['task'],
                                "reminder_old_time_found": selected_reminder['time']
                                # new_time_iso_str_for_update is NOT passed here as user hasn't provided it yet
                            }
                        }
                    ]
                }
                print("Transitioning to ask for new time for selected reminder (update_no_time).")
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
    
    # Handle CaptureTimeIntent for time follow-up
    elif intent_display_name == 'CaptureTimeIntent':
        parameters = req.get('queryResult', {}).get('parameters', {})
        date_time = parameters.get('date-time')

        # Get task from context
        context_list = req.get('queryResult', {}).get('outputContexts', [])
        task = None
        for context in context_list:
            if 'await_time' in context.get('name', ''):
                task = context.get('parameters', {}).get('task')
        if isinstance(task, list):
            task = task[0] if task else None
        if task:
            task = task.strip().lower()

        date_time_str = extract_datetime_str(date_time)

        if task and date_time_str and isinstance(date_time_str, str) and date_time_str.strip():
            try:
                reminder_dt_obj = datetime.fromisoformat(date_time_str)
                reminder_data = {
                    'task': task,
                    'remind_at': reminder_dt_obj,
                    'status': 'pending',
                    'created_at': firestore.SERVER_TIMESTAMP
                }
                db.collection('reminders').add(reminder_data)
                user_friendly_time_str = reminder_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                return jsonify({
                    "fulfillmentText": f"All set! I’ll remind you to {task} at {user_friendly_time_str}. ✅",
                    "outputContexts": [
                        {
                            "name": f"{req['session']}/contexts/await_task",
                            "lifespanCount": 0
                        },
                        {
                            "name": f"{req['session']}/contexts/await_time",
                            "lifespanCount": 0
                        }
                    ]
                })
            except Exception as e:
                print(f"Error saving reminder in CaptureTimeIntent: {e}")
                return jsonify({
                    "fulfillmentText": "Sorry, I couldn't save your reminder. Please try again."
                })
        else:
            return jsonify({
                "fulfillmentText": "Hmm, I’m still missing some info. Could you try again?"
            })

    # Handle CaptureTaskIntent for task follow-up
    elif intent_display_name == 'CaptureTaskIntent':
        parameters = req.get('queryResult', {}).get('parameters', {})
        task = parameters.get('task')

        # Handle task as a list
        if isinstance(task, list):
            task = task[0] if task else None
        if task:
            task = task.strip().lower()

        # --- PATCH: Filter out generic tasks ---
        GENERIC_TASKS = {"set a reminder", "reminder", "remind me", "remind", "add reminder"}
        if task and task in GENERIC_TASKS:
            # Double-clear both contexts, then set a fresh await_task
            return jsonify({
                "fulfillmentText": "Sure! ☺️ What should I remind you about?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_task",
                        "lifespanCount": 0,
                        "parameters": {}
                    },
                    {
                        "name": f"{req['session']}/contexts/await_time",
                        "lifespanCount": 0,
                        "parameters": {}
                    },
                    {
                        "name": f"{req['session']}/contexts/await_task",
                        "lifespanCount": 2,
                        "parameters": {}
                    }
                ]
            })
        # --- END PATCH ---

        # Get time from context
        context_list = req.get('queryResult', {}).get('outputContexts', [])
        date_time = None
        for context in context_list:
            if 'await_task' in context.get('name', ''):
                date_time = context.get('parameters', {}).get('date-time')
        date_time_str = extract_datetime_str(date_time)

        # If only a task is present and no valid date-time, prompt for time and set await_time context
        if task and (not date_time_str or not (isinstance(date_time_str, str) and date_time_str.strip())):
            return jsonify({
                "fulfillmentText": f"Great! What time should I remind you to {task}?",
                "outputContexts": [
                    {
                        "name": f"{req['session']}/contexts/await_time",
                        "lifespanCount": 2,
                        "parameters": {
                            "task": task
                        }
                    }
                ]
            })

        if task and date_time_str and isinstance(date_time_str, str) and date_time_str.strip():
            try:
                reminder_dt_obj = datetime.fromisoformat(date_time_str)
                reminder_data = {
                    'task': task,
                    'remind_at': reminder_dt_obj,
                    'status': 'pending',
                    'created_at': firestore.SERVER_TIMESTAMP
                }
                db.collection('reminders').add(reminder_data)
                user_friendly_time_str = reminder_dt_obj.astimezone(KUALA_LUMPUR_TZ).strftime("%I:%M %p on %B %d, %Y")
                return jsonify({
                    "fulfillmentText": f"All set! ✅ I’ll remind you to {task} at {user_friendly_time_str}.",
                    "outputContexts": [
                        {
                            "name": f"{req['session']}/contexts/await_task",
                            "lifespanCount": 0
                        },
                        {
                            "name": f"{req['session']}/contexts/await_time",
                            "lifespanCount": 0
                        }
                    ]
                })
            except Exception as e:
                print(f"Error saving reminder in CaptureTaskIntent: {e}")
                return jsonify({
                    "fulfillmentText": "Sorry, I couldn't save your reminder. Please try again."
                })
        else:
            if task and (not date_time_str or not (isinstance(date_time_str, str) and date_time_str.strip())):
                return jsonify({
                    "fulfillmentText": f"Great! What time should I remind you to {task}?"
                })
            else:
                return jsonify({
                    "fulfillmentText": "Oops! I need both the task and the time. Could you try again?"
                })

    # Fallback if no specific intent is matched
    return jsonify({
        "fulfillmentText": "I'm not sure how to respond to that yet. Please ask me about setting, deleting, or updating a reminder!"
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port, debug=True)
