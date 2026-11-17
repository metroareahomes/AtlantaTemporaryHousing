"""Plain-language labels and help for Setup. Display text only; no automation rules."""

SETUP_FIELDS = {
    "public_url": {
        "label": "Your Home Assistant web address",
        "brief": "The secure address used to reach Home Assistant from outside your home.",
        "more": "Hostaway sends booking changes to this address. Use the address that starts with https://, without /api/webhook at the end.",
        "example": "https://your-home-assistant.example.com"
    },
    "addon_slug": {
        "label": "This app’s internal name",
        "brief": "Home Assistant uses this name to send booking updates to the correct app.",
        "more": "This is also called the add-on slug. It is usually filled in automatically on Home Assistant Green. Leave it alone unless the app cannot be found.",
        "example": "A name ending in _stay_automation."
    },
    "register_hostaway": {
        "label": "Connect these updates to Hostaway too",
        "brief": "Ask Hostaway to send booking changes to this app.",
        "more": "Checking this box and saving registers the connection in Hostaway. Without live updates, the regular reservation checks still run.",
        "example": "Turn this on when first connecting live booking updates."
    },
    "add_hour": {
        "label": "Add guest codes at this hour (0–23)",
        "brief": "The usual time to put today’s guest codes into the locks.",
        "more": "Use a whole hour on a 24-hour clock. Early arrivals and the final safety check can make the app add a code sooner.",
        "example": "8 = 8 AM; 14 = 2 PM. A guest arriving today normally gets a code added at 8 AM when this is 8."
    },
    "early_lead_hours": {
        "label": "Hours before an early arrival",
        "brief": "Give an early-arriving guest their code this many hours before check-in.",
        "more": "The app uses whichever code-installation time comes first: the usual hour, this many hours before arrival, or the final safety check.",
        "example": "3 means a guest checking in at 9 AM can have their code added at 6 AM."
    },
    "final_check_minutes": {
        "label": "Check guest access this many minutes before arrival",
        "brief": "Make a final check that the guest’s code is in the lock.",
        "more": "If access cannot be confirmed, the app follows your backup-code and guest-message choices below. Checks happen on the app’s next scheduled pass, so this is not an exact-to-the-minute promise.",
        "example": "60 means start the final check one hour before check-in."
    },
    "retry_minutes": {
        "label": "Minutes between failed lock checks",
        "brief": "Wait this long before trying a failed lock operation again.",
        "more": "This controls lock checks and code changes. It is not the wait between sending phone alerts.",
        "example": "15 means try the lock again about 15 minutes later."
    },
    "afternoon_check_hour": {
        "label": "Afternoon lock check hour (0–23)",
        "brief": "Check guest codes again later in the day.",
        "more": "Use a whole hour on a 24-hour clock. This is another chance to catch a missing code before arrival.",
        "example": "14 = 2 PM."
    },
    "daily_check_hour": {
        "label": "Daily lock check hour (0–23)",
        "brief": "The hour for the regular daily lock check.",
        "more": "Use a whole hour on a 24-hour clock. The app checks locks that have automation turned on.",
        "example": "6 = 6 AM."
    },
    "report_hour": {
        "label": "Daily arrivals report hour (0–23)",
        "brief": "The hour to send the day’s arrivals summary.",
        "more": "The report lists today’s arrivals and whether their codes have been confirmed. It uses the saved alert destinations.",
        "example": "7 = 7 AM."
    },
    "stale_check_minutes": {
        "label": "Minutes after checkout before warning about an old code",
        "brief": "Give Hostaway time to remove the departed guest’s code.",
        "more": "After this wait, staff can be warned if the old code is still found. The app does not delete codes that it did not create.",
        "example": "120 means wait two hours after checkout before warning."
    },
    "alert_service": {
        "label": "Where to send your phone alerts",
        "brief": "The Home Assistant notification action for your phone.",
        "more": "Copy the full action name exactly. This is not an email address or phone number. Extra recipients can be added on the Staff page. Save this setting before sending a test alert.",
        "example": "notify.mobile_app_kurts_iphone"
    },
    "backup_codes_enabled": {
        "label": "Keep a backup door code ready",
        "brief": "Put a spare access code into each lock with automation turned on.",
        "more": "This lets the app prepare a backup code for a guest whose personal code cannot be confirmed. This switch alone does not send a message to the guest.",
        "example": "On = prepare the spare code; off = do not manage backup codes."
    },
    "guest_messages_enabled": {
        "label": "Send the backup code to the guest when needed",
        "brief": "Let the app message the guest through Hostaway if their personal code is not confirmed.",
        "more": "A backup code must also be available. When this is off, the guest is not automatically messaged; staff are alerted instead.",
        "example": "On = send the guest a backup-code message when needed."
    },
    "guest_message": {
        "label": "Message the guest gets with the backup code",
        "brief": "Write the message that explains which code the guest should use.",
        "more": "Keep {code} in your message so the guest receives the code. The app replaces {guest} with the guest’s first name, {property} with the home’s name, and {code} with the backup code.",
        "example": "Hi {guest}, please use {code} to enter {property}."
    },
    "occ_summer": {
        "label": "Guest temperature in summer (°F)",
        "brief": "The cooling temperature to prepare for a guest.",
        "more": "Used during the summer months chosen below, for homes with thermostat automation turned on.",
        "example": "72 means cool the home to 72°F for the guest."
    },
    "occ_winter": {
        "label": "Guest temperature in winter (°F)",
        "brief": "The heating temperature to prepare for a guest.",
        "more": "Used outside the summer months chosen below, for homes with thermostat automation turned on.",
        "example": "66 means heat the home to 66°F for the guest."
    },
    "vac_summer": {
        "label": "Empty-home temperature in summer (°F)",
        "brief": "The cooling temperature when no guest is staying.",
        "more": "This must be the same as or warmer than the summer guest temperature.",
        "example": "78 means let an empty home stay at 78°F in summer."
    },
    "vac_winter": {
        "label": "Empty-home temperature in winter (°F)",
        "brief": "The heating temperature when no guest is staying.",
        "more": "This must be the same as or cooler than the winter guest temperature.",
        "example": "55 means keep an empty home at 55°F in winter."
    },
    "therm_lead_hours": {
        "label": "Start guest temperature this many hours before arrival",
        "brief": "Give the home time to warm up or cool down before check-in.",
        "more": "This controls when the app starts preparing the guest temperature. It does not guarantee the home reaches that temperature by check-in.",
        "example": "3 means start at 1 PM for a 4 PM check-in."
    },
    "therm_lag_hours": {
        "label": "Wait this many hours after checkout before using empty-home temperature",
        "brief": "Keep the guest temperature for a little while after checkout.",
        "more": "For a same-day turnover, the app keeps the home ready for the next guest instead of making an empty-home gap.",
        "example": "3 means switch at 1 PM after a 10 AM checkout, unless another guest arrives that day."
    },
    "summer_start_month": {
        "label": "First month to use summer temperatures",
        "brief": "Summer temperatures start in this month.",
        "more": "The selected month is included. Months outside the summer range use winter temperatures. A range can cross New Year.",
        "example": "May through October uses summer temperatures in all six of those months."
    },
    "summer_end_month": {
        "label": "Last month to use summer temperatures",
        "brief": "Keep summer temperatures through the end of this month.",
        "more": "The selected month is included. Winter temperatures start the next month.",
        "example": "October means November uses winter temperatures, if summer starts in May."
    },
    "vacant_reset_hour": {
        "label": "Reset empty homes at this hour (0–23)",
        "brief": "Once a day, put empty homes back to their empty-home temperature.",
        "more": "This helps if someone changed the thermostat while cleaning. It applies to empty homes, not to guests who are staying.",
        "example": "11 = 11 AM."
    },
    "therm_offline_minutes": {
        "label": "Minutes offline before a thermostat warning",
        "brief": "Wait this long before warning that a thermostat is not responding.",
        "more": "A short connection problem should not cause an immediate warning. Staff are alerted when the offline period reaches this limit.",
        "example": "30 means warn after about half an hour offline."
    },
    "therm_retry_minutes": {
        "label": "Minutes before retrying a failed temperature change",
        "brief": "Wait this long before asking a thermostat again after a failed change.",
        "more": "This avoids repeatedly sending requests to a thermostat that is having trouble.",
        "example": "60 means wait about one hour."
    },
    "therm_max_per_tick": {
        "label": "Thermostats to change in each five-minute round",
        "brief": "Limit how many thermostats the app changes at a time.",
        "more": "Keep this at 1 while the thermostat service limits requests. Raising it can cause more failed requests.",
        "example": "1 means change at most one thermostat in each five-minute round."
    },
    "therm_gap_seconds": {
        "label": "Seconds between thermostat changes",
        "brief": "Pause between changes when more than one thermostat is changed in a round.",
        "more": "This pause only matters when the number of thermostats per round is greater than 1.",
        "example": "30 means wait half a minute between changes."
    },
    "battery_alert_percent": {
        "label": "Warn when a lock battery falls below this percent",
        "brief": "Get an alert before a lock battery gets too low.",
        "more": "This is a warning level, not the battery’s current charge. Battery readings depend on what Home Assistant reports.",
        "example": "15 means warn when the reported battery is below 15%."
    },
    "lyric_climate_entities": {
        "label": "Extra Lyric thermostats to include",
        "brief": "Only the listed devices are included. Lyric commands can still refresh its whole cloud account.",
        "more": "Use exact climate entity IDs, separated by spaces or commas. The thermostat must belong to Honeywell Lyric. Save this list, then assign the device to a property manually. This does not change Lyric’s own polling or guarantee protection from cloud rate limits. Leave blank to exclude all Lyric thermostats.",
        "example": "climate.livingroom_thermostat"
    }
}
