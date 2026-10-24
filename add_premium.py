from pyrogram import filters
from datetime import timedelta
from config import OWNER_ID
from shared_client import app
from utils.func import add_premium_user, is_private_chat


@app.on_message(filters.command("add"))
async def add_premium_handler(client, message):
    """Handle /add command to add premium users (owner only)"""
    if not await is_private_chat(message):
        await message.reply_text("This command can only be used in private chats for security reasons.")
        return

    user_id = message.from_user.id

    # Check if sender is owner
    if user_id not in OWNER_ID:
        await message.reply_text("This command is restricted to the bot owner.")
        return

    text = (message.text or "").strip()
    parts = text.split()  # handles any amount of whitespace

    # Validate command format
    if len(parts) != 4:
        await message.reply_text("Invalid format. Use: /add user_id duration_value duration_unit\n"
                                 "Example: /add 123456 1 week")
        return

    try:
        target_user_id = int(parts[1])
        duration_value = int(parts[2])
        duration_unit = parts[3].lower()

        # Validate duration unit
        valid_units = ["min", "hours", "days", "weeks", "month", "year", "decades"]
        if duration_unit not in valid_units:
            await message.reply_text(f"Invalid duration unit. Choose from: {', '.join(valid_units)}")
            return

        # Add premium user
        success, result = await add_premium_user(target_user_id, duration_value, duration_unit)

        if success:
            # Format expiry date in Asia/Kolkata timezone
            expiry_utc = result
            expiry_ist = expiry_utc + timedelta(hours=5, minutes=30)  # IST is UTC+5:30
            formatted_expiry = expiry_ist.strftime("%d-%b-%Y %I:%M:%S %p")

            await message.reply_text(f"✓ User {target_user_id} added as premium member\n"
                                     f"Subscription valid until: {formatted_expiry} (IST)")
            try:
                await client.send_message(target_user_id, f"✓ You have been added as premium member\n\n"
                                                          f"**Validity upto**: {formatted_expiry} (IST)")
            except Exception as e:
                print(f"Could not notify user {target_user_id} about premium: {e}")
        else:
            await message.reply_text(f"✕ Failed to add premium user: {result}")

    except ValueError:
        await message.reply_text("Invalid user ID or duration value. Both must be integers.")
    except Exception as e:
        await message.reply_text(f"Error: {str(e)}")
