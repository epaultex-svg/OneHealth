
## API reference docs: https://docs.nexhealth.com/reference/introduction

# Getting started with scheduling

This guide will help you use the NexHealth Synchronizer API to book an appointment for a new or existing patient. The NexHealth Synchronizer API makes it easy to embed online booking into your application and start driving more revenue for practices.

This guide is written with the assumption that you already have a sandbox environment set up and bearer token ready. If you don't have access request a sandbox [here](https://www.nexhealth.com/api-request/request-access) and if you don't have a bearer token check out our doc on authentication [here](https://docs.nexhealth.com/reference/authentication-1).

We've also put together an accompanying video tutorial below.

<Embed url="https://www.youtube.com/playlist?list=PLL2Iy1oGVcCacoZ8VsvqKKnzRjNJ6R1ca" title="NexHealth API tutorial: Booking an appointment" favicon="https://www.youtube.com/s/desktop/a98f809d/img/favicon.ico" image="https://i.ytimg.com/vi/R2nLe-ZFnn8/hqdefault.jpg?sqp=-oaymwEWCKgBEF5IWvKriqkDCQgBFQAAiEIYAQ==&rs=AOn4CLCDvpFVr5Y42Tv9mYO42bXie49Gqg&days_since_epoch=19367" provider="youtube.com" href="https://www.youtube.com/playlist?list=PLL2Iy1oGVcCacoZ8VsvqKKnzRjNJ6R1ca" html="%3Ciframe%20class%3D%22embedly-embed%22%20src%3D%22%2F%2Fcdn.embedly.com%2Fwidgets%2Fmedia.html%3Fsrc%3Dhttp%253A%252F%252Fwww.youtube.com%252Fembed%252Fvideoseries%253Flist%253DPLL2Iy1oGVcCacoZ8VsvqKKnzRjNJ6R1ca%26display_name%3DYouTube%26url%3Dhttps%253A%252F%252Fwww.youtube.com%252Fplaylist%253Flist%253DPLL2Iy1oGVcCacoZ8VsvqKKnzRjNJ6R1ca%26image%3Dhttps%253A%252F%252Fi.ytimg.com%252Fvi%252FR2nLe-ZFnn8%252Fhqdefault.jpg%253Fsqp%253D-oaymwEWCKgBEF5IWvKriqkDCQgBFQAAiEIYAQ%253D%253D%2526rs%253DAOn4CLCDvpFVr5Y42Tv9mYO42bXie49Gqg%2526days_since_epoch%253D19367%26key%3Df2aa6fc3595946d0afc3d76cbbd25dc3%26type%3Dtext%252Fhtml%26schema%3Dyoutube%22%20width%3D%22853%22%20height%3D%22480%22%20scrolling%3D%22no%22%20title%3D%22YouTube%20embed%22%20frameborder%3D%220%22%20allow%3D%22autoplay%3B%20fullscreen%22%20allowfullscreen%3D%22true%22%3E%3C%2Fiframe%3E" />

## Overview

To book an appointment you need to understand the who, what, where, and when of the booking.

* Who: The patient who is attending the appointment and the provider who will be providing care.
* What: What kind of appointment is being booked
* Where: What location the appointment will occur in.
* When: What time the appointment should occur.

## Who

To avoid creating duplicate patients in a practice's health record system, you should always look for an existing patient before creating an appointment. You can do this with a Get request to the /patients as shown below.

Make sure to fill in your subdomain, location, and bearer token.

```curl
curl --request GET \
     --url 'https://nexhealth.info/patients?subdomain=YOUR_SUBDOMAIN&location_id=YOUR_LOCATION&new_patient=false&include_upcoming_appts=false&location_strict=false&page=1&per_page=5' \
     --header 'Accept: application/vnd.Nexhealth+json;version=2' \
     --header 'Nex-Api-Version: v20240412' \
     --header 'Authorization: Bearer YOUR_BEARER_TOKEN'
```

You will get back a response as shown below. Make sure to pick a patient you'd like to book an appointment with and store their ID.

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
      "patients": [
        {
          "id": 415,
          "email": "Amy.Ramos@nexhealth.com",
          "first_name": "John",
          "middle_name": "Anthony",
          "last_name": "Smith",
          "name": "John Smith",
          "display_name": "John Smith",
          "doctor_name": "Dr. John Smith",
          "meta_type": "Dentist",
          "created_at": "2020-06-05T20:16:57.007Z",
          "updated_at": "2020-06-05T20:16:57.007Z",
          "profile_url": "https://storage.googleapis.com/nexassets/app/img/icon/avatar.svg",
          "foreign_id": 0,
          "foreign_id_type": "--DataSource-",
          "npi": "string",
          "inst_ids": [
            [
              1,
              2
            ]
          ],
          "bio": {
            "phone_number": "5163042196",
            "date_of_birth": "1964-05-03"
          },
          "unsubscribe_emails": true,
          "inactive": false,
          "last_sync": true,
          "last_sync_time": "string",
          "unsubscribe_sms": true,
          "last_check_in": "2020-06-05T20:16:57.007Z",
          "prov_ids": [
            [
              504
            ]
          ],
          "invalid_email": [
            [
              ""
            ]
          ],
          "contact_email": "string",
          "guarantor_id": 0,
          "pcp_id": 504,
          "last_visit": {},
          "upcoming_appts": [
            {
              "id": 1822,
              "provider_id": 102,
              "provider_name": "Dr. John Smith",
              "start_time": "2021-12-06T09:45:00.000Z",
              "end_time": "2021-12-06T10:00:00.000Z",
              "location_id": 1,
              "confirmed": true
            }
          ]
        }
      ]
    }
  ],
  "count": 2
}
```

If you would like to book an appointment for a new patient, create a new patient with a [Post request](https://docs.nexhealth.com/reference/postpatients) and store the newly created patient ID.

Next we have to pick a provider. You can do this with a Get request to /providers as shown below.

Make sure to fill in your subdomain and bearer token.

```curl
curl --request GET \
     --url 'https://nexhealth.info/providers?subdomain=YOUR_SUBDOMAIN&page=1&per_page=5' \
     --header 'Accept: application/vnd.Nexhealth+json;version=2' \
     --header 'Nex-Api-Version: v20240412' \
     --header 'Authorization: Bearer YOUR_BEARER_TOKEN'
```

Make sure to pick a provider you'd like to book an appointment with and store their ID for later.

## What

To book an appointment via NexHealth you have to specify what kind of appointment it is via our appointment types. You can use appointment types to control the length of the appointment as well as what procedure codes are mapped to that appointment when created.

To create an appointment type you can make a Post request to /appointment\_types as shown below.

Make sure to fill in your subdomain, location, and bearer token.

```curl
curl --request POST \
     --url 'https://nexhealth.info/appointment_types?subdomain=YOUR_SUBDOMAIN' \
     --header 'Accept: application/vnd.Nexhealth+json;version=2' \
     --header 'Authorization: Bearer YOUR_TOKEN' \
     --header 'Nex-Api-Version: v20240412' \
     --header 'Content-Type: application/json' \
     --data '
{
     "appointment_type": {
          "name": "New Patient",
          "minutes": 30,
          "parent_type": "Location",
          "parent_id": "YOUR_LOCATION"
     },
     "location_id": "YOUR_LOCATION"
}
'
```

In the response you will get back an appointment type ID. Keep track of that for later.

## Where

You should have received a location ID with your API key. Each location represents a physical office. If you don't have the location ID handy you can query all locations within your subdomain with a Get request to /locations as shown below.

Make sure to fill in your subdomain and bearer token.

```curl
curl --request GET \
     --url 'https://nexhealth.info/locations?subdomain=YOUR_SUBDOMAIN' \
     --header 'Accept: application/vnd.Nexhealth+json;version=2' \
     --header 'Nex-Api-Version: v20240412' \
     --header 'Authorization: Bearer YOUR_BEARER_TOKEN'
```

Now you have the location where the appointment will be booked.

If you only want to surface appointments in a specific room you can query the operatories in your location as shown below with a Get request to /operatories.

Make sure to fill in your subdomain, location, and bearer token.

```curl
curl --request GET \
     --url 'https://nexhealth.info/operatories?subdomain=YOUR_SUBDOMAIN&location_id=YOUR_LOCATION&page=1&per_page=5' \
     --header 'Accept: application/vnd.Nexhealth+json;version=2' \
     --header 'Nex-Api-Version: v20240412' \
     --header 'Authorization: Bearer YOUR_BEARER_TOKEN'
```

## When

To create an opening in a provider schedule where appointments can be booked, you have to create an availability. This allows you to ensure that providers are only being booked for appointments when they want them.

You can create an availability with a Post request to /availabilities. There is a body object to attach this time, where you specify that this availability is Mondays and Tuesdays from 9-5 with the provider, and appointment type you decided on earlier.

As always make sure to fill in your subdomain, location, type ID, and bearer token.

```curl
curl --request POST \
     --url 'https://nexhealth.info/availabilities?subdomain=YOUR_SUBDOMAIN&location_id=YOUR_LOCATION' \
     --header 'Accept: application/vnd.Nexhealth+json;version=2' \
     --header 'Authorization: Bearer YOUR_BEARER_TOKEN' \
     --header 'Nex-Api-Version: v20240412' \
     --header 'Content-Type: application/json' \
     --data '
{
     "availability": {
          "days": [
               "Monday",
               "Tuesday"
          ],
          "appointment_type_ids": [
               "YOUR_TYPE"
          ],
          "active": true,
          "provider_id": YOUR_PROVIDER,
          "begin_time": "09:00",
          "end_time": "05:00"
     }
}
```

Now you can use our utility endpoint, Get /appointment\_slots to return available slots for the provider and location you decided upon above.

Make sure to fill in your provider, location, start\_date, subdomain, location, and bearer token.

```json
curl --request GET \
     --url 'https://nexhealth.info/appointment_slots?subdomain=YOUR_SUBDOMAIN&start_date=2021-04-18&days=10&lids\[\]=YOUR_LOCATION&pids\[\]=YOUR_PROVIDER' \
     --header 'Accept: application/vnd.Nexhealth+json;version=2' \
     --header 'Nex-Api-Version: v20240412' \
     --header 'Authorization: Bearer YOUR_BEARER_TOKEN'
```

You should get a response like the one below with times that are available to be booked. Pick one noting the time and operatory ID and lets create this appointment!

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

## Creating an appointment

Putting it all together you can create an appointment with a Post request to /appointments.

Make sure to include everything you pulled above subdomain, location, bearer token, patient, provider, type ID, operatory, and start time.

```curl
curl --request POST \
     --url 'https://nexhealth.info/appointments?subdomain=YOUR_SUBDOMAIN&location_id=YOUR_LOCATION&notify_patient=false' \
     --header 'Accept: application/vnd.Nexhealth+json;version=2' \
     --header 'Authorization: Bearer YOUR_BEARER_TOKEN' \
     --header 'Nex-Api-Version: v20240412' \
     --header 'Content-Type: application/json' \
     --data '
{
     "appt": {
          "patient_id": "YOUR_PATIENT",
          "provider_id": "YOUR_PROVIDER",
          "operatory_id": "YOUR_OPERATORY",
          "start_time": "YOUR_STARTTIME"
          "appointment_type_id": YOUR_TYPE
     }
}
'
```