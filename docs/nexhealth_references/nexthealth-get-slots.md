Available Slots

# Available Slots

Available slots provides a query interface for API users to retrieve valid start times for booking an appointment. Available slots require a subdomain to be provided and accepts an array of location ids or `lids` and provider ids or `pids`, so you can retrieve 'slots' for more than one location and provider at a time.

> 📘 Multiple operatories
>
> If the same time slot is available in multiple operatories, only one slot will be returned.

> 👍 next\_available\_date
>
> If no slots are found, the `next_available_date` property will contain the next day with valid slots. If there are no valid slots within 180 days this field will be `null`.

> 🚧 Use smaller number of days for better performance
>
> Response time increases when requesting a large number of days. For better performance, request a smaller number of days and leverage the `next_available_date` field if no slots are found.

## Slot length and slot interval

Slot length is the length of availability you are looking to receive viable start times for. The endpoint will respond with possible start times for a 15 minute appointment by default. You can set this yourself by providing a `slot_length` parameter. Additionally, if a `category_id` is provided, `slot_length` will be set to the duration of that category for each provider/operatory.

If you provide both `category_id` and `slot_length` the length of the `category_id` takes precedence over `slot_length`.

Slot interval is the frequency with which to validate start times. The default is to check in increments matching the slot\_length in minutes. So if slot\_length is set to 60 minutes, then valid appointment start times will only be returned in 60 minute increments even if you could potentially book an appointment 5 minutes after a returned slot. You may set this manually by specifying the `slot_interval` parameter.

> 📘 Timezones
>
> Take note that slots times are always queried and returned in the location's local timezone.

## Available slots response

```json
{
  "code": false,
  "description": [
    [
      "Description"
    ]
  ],
  "error": [
    [
      "Error"
    ]
  ],
  "data": [
    {
      "lid": 1,
      "pid": 0,
      "operatory_id": 54,
      "slots": [
        {
          "time": "2017-10-09T07:00:00.000-04:00",
          "operatory_id": 0,
          "provider_id": 83
        }
      ]
    }
  ],
  "count": 2
}
```