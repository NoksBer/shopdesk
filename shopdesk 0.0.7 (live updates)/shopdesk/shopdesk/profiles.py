"""Shop-type profiles: terminology, equipment fields, statuses and templates applied during onboarding.

One shared data model; profiles only change labels, default fields and suggested workflow.
"""

# (name, customer-facing label, is_closed)
DEFAULT_STATUSES = [
    ("New / Checked In", "Received", 0),
    ("Awaiting Diagnosis", "Diagnosis in progress", 0),
    ("Awaiting Customer Approval", "Waiting for your approval", 0),
    ("Approved / Ready for Repair", "Approved", 0),
    ("In Progress", "Repair in progress", 0),
    ("Waiting for Parts", "Waiting for parts", 0),
    ("Quality Check / Testing", "Testing", 0),
    ("Ready for Pickup", "Ready for pickup", 0),
    ("Completed / Closed", "Completed", 1),
    ("Cancelled", "Cancelled", 1),
]


def _f(key, label, kind="text"):
    return {"key": key, "label": label, "type": kind}


PROFILES = {
    "computer": {
        "label": "Computer / electronics repair",
        "terminology": {"item": "Device", "items": "Devices"},
        "equipment_fields": [
            _f("device_type", "Device type"), _f("manufacturer", "Manufacturer"), _f("model", "Model"),
            _f("serial_number", "Serial number"), _f("operating_system", "Operating system"),
            _f("accessories", "Accessories received"),
        ],
        "intake_checklist": ["Power adapter received", "Password / PIN noted", "Data backup discussed", "Photos taken"],
        "service_templates": ["Diagnostic fee", "OS reinstall", "Virus / malware removal", "Data backup", "Screen replacement"],
    },
    "phone": {
        "label": "Phone / tablet repair",
        "terminology": {"item": "Device", "items": "Devices"},
        "equipment_fields": [
            _f("manufacturer", "Manufacturer"), _f("model", "Model"), _f("imei_serial", "IMEI / serial number"),
            _f("damage", "Damage / condition"), _f("accessories", "Accessories received"),
        ],
        "intake_checklist": ["Passcode removed or noted", "Find My / account lock off", "SIM and case removed", "Pre-existing damage photographed"],
        "service_templates": ["Screen replacement", "Battery replacement", "Charge port repair", "Water damage assessment"],
    },
    "bicycle": {
        "label": "Bicycle repair",
        "terminology": {"item": "Bike", "items": "Bikes"},
        "equipment_fields": [
            _f("bike_type", "Bike type"), _f("brand", "Brand"), _f("model", "Model"),
            _f("frame_number", "Frame number"), _f("wheel_size", "Wheel size"),
            _f("components", "Components and condition"), _f("requested_service", "Requested service"),
        ],
        "intake_checklist": ["Accessories (lights, bags) noted", "Tire condition checked", "Brakes checked", "Photos taken"],
        "service_templates": ["Basic tune-up", "Full overhaul", "Flat repair", "Brake adjustment", "Drivetrain clean"],
    },
    "small_engine": {
        "label": "Small engine / equipment repair",
        "terminology": {"item": "Equipment", "items": "Equipment"},
        "equipment_fields": [
            _f("equipment_type", "Equipment type"), _f("manufacturer", "Manufacturer"), _f("model", "Model"),
            _f("engine_model", "Engine model"), _f("serial_number", "Serial number"),
            _f("engine_hours", "Engine hours", "number"), _f("fuel_condition", "Fuel / equipment condition"),
        ],
        "intake_checklist": ["Fuel drained or noted", "Blade / attachments removed", "Spark plug checked", "Photos taken"],
        "service_templates": ["Annual tune-up", "Carburetor clean", "Blade sharpening", "Winterization"],
    },
    "appliance": {
        "label": "Appliance repair",
        "terminology": {"item": "Appliance", "items": "Appliances"},
        "equipment_fields": [
            _f("appliance_type", "Appliance type"), _f("brand", "Brand"), _f("model", "Model"),
            _f("serial_number", "Serial number"), _f("symptoms", "Symptoms"),
        ],
        "intake_checklist": ["Model plate photographed", "Warranty status checked", "Access to appliance confirmed"],
        "service_templates": ["Service call", "Diagnostic fee", "Part replacement labor"],
    },
    "general": {
        "label": "General repair",
        "terminology": {"item": "Item", "items": "Items"},
        "equipment_fields": [
            _f("item_type", "Item type"), _f("manufacturer", "Manufacturer / brand"), _f("model", "Model"),
            _f("serial_number", "Serial / identifier"), _f("condition", "Condition at intake"),
        ],
        "intake_checklist": ["Photos taken", "Accessories noted"],
        "service_templates": ["Diagnostic fee", "Standard labor"],
    },
    "custom": {
        "label": "Other / custom",
        "terminology": {"item": "Item", "items": "Items"},
        "equipment_fields": [
            _f("item_type", "Type"), _f("model", "Model"), _f("serial_number", "Serial / identifier"),
        ],
        "intake_checklist": [],
        "service_templates": [],
    },
}


def shop_type_list():
    return [{"key": k, "label": v["label"]} for k, v in PROFILES.items()]
