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

SETUP_FIELDS.update({
    "occupied_reset_enabled": {
        "label": "Check occupied-home temperatures once a day",
        "brief": "Turn this on to gently limit high heating or low cooling while guests are staying.",
        "more": "Off by default. Applies to active homes already marked for thermostat automation, on full days between arrival and departure. Each thermostat is checked once per day after the hour below. A reasonable setting is left alone. This does not change arrival or vacant temperatures.",
        "example": "Summer cooling at 68°F becomes 73°F; cooling at 75°F stays at 75°F."
    },
    "daily_winter": {
        "label": "Winter: highest reasonable heating setting (°F)",
        "brief": "Lower the heating setting to this number only when it is higher. Default: 70°F.",
        "more": "This is the thermostat setting, not the measured room temperature. Uses your winter months. When adjustment is needed, Heat mode is confirmed first. Settings at or below this number are left alone.",
        "example": "At 70°F: heating set to 75°F becomes 70°F; heating set to 68°F stays 68°F."
    },
    "daily_summer": {
        "label": "Summer: lowest reasonable cooling setting (°F)",
        "brief": "Raise the cooling setting to this number only when it is lower. Default: 73°F.",
        "more": "This is the thermostat setting, not the measured room temperature. Uses your summer months. When adjustment is needed, Cool mode is confirmed first. Settings at or above this number are left alone.",
        "example": "At 73°F: cooling set to 68°F becomes 73°F; cooling set to 75°F stays 75°F."
    },
    "occupied_reset_hour": {
        "label": "Daily occupied-home check hour (0–23)",
        "brief": "Choose a whole hour using the local schedule. Default: 10 AM.",
        "more": "The queue starts after this hour on full days between arrival and departure. Arrival and checkout days never qualify, even with early check-in or late checkout. Homes are staggered rather than changed together. Saving again does not repeat a completed check.",
        "example": "10 = 10 AM; 14 = 2 PM."
    },
    "temperature_complaints_enabled": {
        "label": "Pause daily resets when a guest complains about temperature",
        "brief": "Read incoming messages for temperature complaints, without AI tokens.",
        "more": "Keyword rules are imperfect. A match pauses only this reservation’s daily reset until checkout, including extensions, and adds NTC to its Guest Notes. Staff can also enter NTC in Guest Notes to pause the stay. Staff and outgoing messages are ignored. New guests start fresh.",
        "example": "The thermostat keeps resetting, please stop changing it."
    },
    "temperature_pause_field_id": {
        "label": "Optional extra Hostaway pause field number",
        "brief": "NTC in Guest Notes works without this. Leave 0 unless you also use a custom field.",
        "more": "Create a private reservation text field in Hostaway named Pause daily temperature reset. Put YES in it to pause this stay. Use its numeric field ID here, not its name. The app reads this optional field too; automatic pauses are saved locally and add NTC to Guest Notes. Once paused, a stay remains paused through checkout.",
        "example": "If the field ID is 167, enter 167 here. Staff can then enter YES on a reservation."
    }
})

SETUP_FIELDS["occupied_reset_spacing_seconds"] = {
    "label": "Seconds between daily thermostat adjustments",
    "brief": "Spread changes across all homes. Default: at least 90 seconds between thermostat commands.",
    "more": "One thermostat at a time, including homes with multiple thermostats. A setting that is already reasonable needs no cloud command. Daily resets also wait after arrival or vacant-home commands. A failed or uncertain daily adjustment pauses the queue using the existing thermostat retry delay. Other apps and the integration may still make their own cloud requests.",
    "example": "90 = one adjustment about every 1½ minutes. Use 180 or 300 for a slower rollout."
}

SETUP_FIELDS.update({
    'smtp_enabled': dict(label='Send staff alerts by email', brief='Enable email after entering your server settings.', more='Recipients and their owner/category choices are on Staff & access. App notifications can continue too.', example='Turn on after saving your SMTP server and recipients.'),
    'smtp_host': dict(label='Outgoing email server (SMTP)', brief='Your email provider supplies this server address.', more='Use only the SMTP hostname, without https://. TLS encryption is required.', example='smtp.example.com'),
    'smtp_port': dict(label='Email server port', brief='Usually 587 for STARTTLS or 465 for SSL.', more='Use the port specified by your email provider.', example='587'),
    'smtp_security': dict(label='Email encryption', brief='Use the encryption method required by your provider.', more='Certificates are verified. Unencrypted email login is not supported.', example='STARTTLS with port 587.'),
    'smtp_username': dict(label='Email login username', brief='Usually the full email address you sign in with.', more='Re-enter the password if you change the server or username.', example='alerts@example.com'),
    'smtp_password': dict(label='Email password or app password', brief='Leave blank to keep the saved password. It is never displayed.', more='Use a provider app password when required. It is stored in the app configuration, which administrators and backups can access.', example='Paste the SMTP app password from your email provider.'),
    'smtp_from': dict(label='Send alerts from this email address', brief='The sender address your SMTP account is allowed to use.', more='The From address can differ from the login only if your provider permits it.', example='alerts@example.com'),
    'alert_properties_url': dict(label='Permanent link to the Properties list', brief='The HTTPS link recipients should open for more information.', more='Use a permanent authenticated link, never a temporary /api/hassio_ingress/ session URL. For Home Assistant use its external address followed by /hassio/ingress/APP_SLUG?stay_page=properties. Recipients still need their own Home Assistant access.', example='https://your-ha.example.com/hassio/ingress/1777083f_stay_automation?stay_page=properties')
})


SETUP_FIELDS.update({
    "sheet_enabled": {
        "label": "Keep the shared sheet up to date",
        "brief": "Send a view-only copy of property status to Google every five minutes.",
        "more": "This uses saved readings. It does not change a home or ask devices to refresh. Turn it off to stop future updates; existing sheet rows remain.",
        "example": "Turn this on after adding the Google receiver address and connection key."
    },
    "sheet_endpoint": {
        "label": "Where should this app send the sheet updates?",
        "brief": "Paste the web app address from the Google setup script, ending in /exec.",
        "more": "This is the receiver address from Deploy in Apps Script. It is different from the Google spreadsheet link you share with people.",
        "example": "https://script.google.com/macros/s/your-deployment/exec"
    },
    "sheet_secret": {
        "label": "Secret connection key from Google",
        "brief": "Copy CONNECTION_KEY from the script’s Project Settings. Leave blank to keep your saved key.",
        "more": "The key proves that updates came from this app. Keep it private. Enter it again if you change the receiver address; it is never shown after saving.",
        "example": "Paste the full connection key here, never into a shared sheet cell."
    }
})


# Pricing controls affect manual simulations only until the one-property live test is approved.
SETUP_FIELDS.update({
    'pricing_simulation_enabled': dict(label='Allow manual pricing simulations', brief='A simulation reads rates and shows a proposal. It does not change prices.', more='Turn this off to disable the simulation button. Recurring runs and live price writes are unavailable in this version.', example='Enable after choosing one test property.'),
    'pricing_property': dict(label='One home to test', brief='Only this property is read by the pricing preview.', more='Choose one active Hostaway listing. This does not change its lock or thermostat automation.', example='Choose your test home, then save before running a simulation.'),
    'pricing_discount': dict(label='Reduce eligible nightly rates by (%)', brief='Default: 10%. Each proposal uses the current Hostaway calendar rate.', more='This is the percentage reduction, not a new base rate. Simulations never write the discounted value back, so repeating a preview does not compound it.', example='$250 with 10% off gives $225.'),
    'pricing_threshold': dict(label='Only discount rates greater than ($)', brief='Default: $200. A rate equal to this number is excluded.', more='This is an eligibility threshold, not a floor for the final rate. Prices below this threshold after discount are allowed in the proposal.', example='$210 is eligible and becomes $189 at 10% off; $200 is excluded.'),
    'pricing_window': dict(label='Check-in within the next (hours)', brief='Default: 24 hours from the simulated run time.', more='Uses the listing’s normal check-in hour and local calendar date, not just midnight. Already-passed check-in times are excluded.', example='Friday at 2 PM includes a Friday 3 PM check-in; Saturday 3 PM is 25 hours away and excluded.'),
    'pricing_preview_at': dict(label='Pretend the check runs at this Eastern time', brief='Choose the date and time to simulate, within 14 days of now.', more='Future simulations read today’s current calendar prices for those dates. They are not forecasts of future PriceLabs updates. Eastern daylight saving time is applied automatically.', example='Choose the coming Friday at 14:00 to preview activation.'),
})
for _event, _label, _example in [('start','Promotion starts','Friday at 14:00 (2 PM)'), ('check1','Second pricing check','Saturday at 06:00 (6 AM)'), ('check2','Third pricing check','Sunday at 06:00 (6 AM)'), ('end','Promotion stops','Monday at 00:01 (12:01 AM)')]:
    SETUP_FIELDS['pricing_'+_event+'_day'] = dict(label=_label+' — day', brief='All promotion times use America/New_York (Eastern).', more='These settings describe the simulation schedule. Saving them does not enable recurring jobs. Checks and stop must follow the start in order within one week.', example=_example)
    SETUP_FIELDS['pricing_'+_event+'_time'] = dict(label=_label+' — time', brief='Use a 24-hour Eastern time, such as 06:00 or 14:00.', more='Eastern daylight saving time is handled automatically. This is only a planned schedule until the one-property test and activation are approved.', example=_example)

SETUP_FIELDS['pricing_manual_discount'] = dict(
    label='Tonight only — discount (%)', brief='Leave blank to keep tonight’s rate.',
    more='The one-time preview uses the current calendar price and bypasses the weekend schedule and price threshold.',
    example='Enter 20 to preview a 20% reduction for tonight.',
)
for _day, _label in enumerate(('Tonight', 'Tomorrow', 'Two days from today')):
    SETUP_FIELDS[f'pricing_minimum_{_day}'] = dict(
        label=f'{_label} — minimum nights', brief='Leave blank to keep the current minimum.',
        more='This is the minimum length of a reservation arriving on this date.',
        example='Enter 3 to require a stay of at least three nights for arrivals on this date.',
    )
