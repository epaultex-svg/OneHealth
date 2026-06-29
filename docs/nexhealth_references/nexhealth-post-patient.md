Create patient

# Create patient

This endpoint creates a new patient or returns an existing patient if return_existing_if_match is true.

> 🚧 Patient insertion
>
> Patients only get inserted into health record systems when they book their first appointment. Until then, they're just stored in NexHealth's database and their `foreign_id_type` is set to 'nex'.

# OpenAPI definition

```json
{
  "openapi": "3.0.0",
  "info": {
    "title": "NexHealth API",
    "description": "Welcome to the developer hub and documentation for NexHealth API. This section of guide describes the operations, response parameters, request parameters, and parameter constraints related to User API. The term Operations refer to functions or methods. The operations are included in requests and send to the web server. Each operation performs a different action or a query on database.",
    "termsOfService": "https://www.nexhealth.com/terms-of-service",
    "contact": {
      "name": "NexHealth",
      "email": "info@nexhealth.com"
    },
    "license": {
      "name": "NexHealth License 1.0",
      "url": "https://www.nexhealth.com/privacy"
    },
    "version": "v20240412"
  },
  "security": [
    {
      "Authorization": []
    }
  ],
  "tags": [
    {
      "name": "Patients",
      "description": "A patients resource"
    }
  ],
  "paths": {
    "/patients": {
      "post": {
        "summary": "Create patient",
        "description": "This endpoint creates a new patient or returns an existing patient if return_existing_if_match is true.",
        "parameters": [
          {
            "in": "header",
            "name": "Nex-Api-Version",
            "description": "The NexHealth API version",
            "required": true,
            "schema": {
              "type": "string",
              "default": "v20240412"
            }
          },
          {
            "in": "query",
            "name": "subdomain",
            "description": "Used to scope the request to the specified institution",
            "required": true,
            "schema": {
              "type": "string"
            }
          },
          {
            "in": "query",
            "name": "location_id",
            "description": "Used to scope the request to the specified location",
            "required": true,
            "schema": {
              "type": "integer",
              "format": "int32"
            }
          }
        ],
        "requestBody": {
          "content": {
            "application/json": {
              "schema": {
                "$ref": "#/components/schemas/postPatients"
              }
            }
          },
          "required": true
        },
        "responses": {
          "201": {
            "description": "Successful",
            "content": {
              "application/json": {
                "schema": {
                  "$ref": "#/components/schemas/API_V2_Entities_PatientsResponses_Create_Response"
                }
              }
            }
          },
          "400": {
            "description": "Bad Request",
            "content": {
              "application/json": {
                "schema": {
                  "$ref": "#/components/schemas/API_Errors_BadRequest"
                }
              }
            }
          },
          "401": {
            "description": "Unauthorized",
            "content": {
              "application/json": {
                "schema": {
                  "$ref": "#/components/schemas/API_Errors_Unauthorized"
                }
              }
            }
          },
          "403": {
            "description": "Forbidden",
            "content": {
              "application/json": {
                "schema": {
                  "$ref": "#/components/schemas/API_Errors_Forbidden"
                }
              }
            }
          },
          "404": {
            "description": "Not Found",
            "content": {
              "application/json": {
                "schema": {
                  "$ref": "#/components/schemas/API_Errors_NotFound"
                }
              }
            }
          },
          "500": {
            "description": "Internal Server Error",
            "content": {
              "application/json": {
                "schema": {
                  "$ref": "#/components/schemas/API_Errors_InternalServerError"
                }
              }
            }
          }
        },
        "tags": [
          "Patients"
        ],
        "operationId": "postPatients"
      }
    }
  },
  "servers": [
    {
      "url": "https://nexhealth.info"
    }
  ],
  "components": {
    "securitySchemes": {
      "Authorization": {
        "type": "apiKey",
        "name": "Authorization",
        "in": "header"
      }
    },
    "schemas": {
      "API_V2_Entities_PatientBasic": {
        "type": "object",
        "properties": {
          "id": {
            "type": "integer",
            "format": "int32",
            "example": 415,
            "description": "User id"
          },
          "email": {
            "type": "string",
            "example": "Amy.Ramos@nexhealth.com",
            "description": "User email"
          },
          "first_name": {
            "type": "string",
            "example": "John",
            "description": "First name"
          },
          "middle_name": {
            "type": "string",
            "example": "Anthony",
            "description": "Middle name"
          },
          "last_name": {
            "type": "string",
            "example": "Smith",
            "description": "Last name"
          },
          "name": {
            "type": "string",
            "example": "John Smith",
            "description": "Full name"
          },
          "created_at": {
            "type": "string",
            "format": "date-time",
            "example": "2020-06-05T20:16:57.007Z",
            "description": "User creation date in UTC"
          },
          "updated_at": {
            "type": "string",
            "format": "date-time",
            "example": "2020-06-05T20:16:57.007Z",
            "description": "User last updation date in UTC"
          },
          "institution_id": {
            "type": "integer",
            "format": "int32",
            "description": "The institution this user belongs to"
          },
          "foreign_id": {
            "type": "string",
            "description": "Foreign Id is a unique identifier from the integrated system"
          },
          "foreign_id_type": {
            "type": "string",
            "example": "--DataSource-",
            "description": "Foreign Id type is a unique string identifier for the integrated system"
          },
          "bio": {
            "type": "object",
            "example": {
              "city": "New York",
              "state": "NY",
              "gender": "Female",
              "zip_code": "20814",
              "new_patient": false,
              "non_patient": true,
              "phone_number": "5163042196",
              "date_of_birth": "1964-05-03",
              "address_line_1": "",
              "address_line_2": "",
              "street_address": "",
              "cell_phone_number": "",
              "home_phone_number": "",
              "work_phone_number": ""
            },
            "description": "Patient biographical data, fields shown in our example response represent all possible data we retrieve but depending on system and what is actually saved in the health records system you cannot assume any field will consistently be returned"
          },
          "inactive": {
            "type": "boolean",
            "example": false,
            "description": "Is the user inactivated?"
          },
          "last_sync_time": {
            "type": "string",
            "format": "date-time",
            "description": "Last time the resource was refreshed with data from the data source"
          },
          "guarantor_id": {
            "type": "integer",
            "format": "int32",
            "description": "User id of this patient's responsible party"
          },
          "billing_type": {
            "type": "string",
            "example": "Standard Billing - finance charges",
            "description": "Used by practices in some integrated systems to categorize and filter patients when creating reports, requesting payments, and performing other related office tasks. Some integrated systems call this an account type rather than a billing type"
          },
          "chart_id": {
            "type": "string",
            "example": "017407",
            "description": "User-facing ID for referencing patient data, used in some integrated systems. Depending on the system, the chart ID supplements or replaces the foreign_id as the ID visible to EHR users"
          },
          "preferred_language": {
            "type": "string",
            "example": "es",
            "description": "Patient's preferred language as an ISO 639-1 code, if specified in the integrated system"
          },
          "preferred_locale": {
            "type": "string",
            "example": "es"
          },
          "location_ids": {
            "type": "array",
            "items": {
              "type": "integer",
              "format": "int32"
            },
            "example": [
              101,
              102,
              103
            ],
            "description": "Array of location ids associated with the user"
          }
        }
      },
      "API_Errors_BadRequest": {
        "type": "object",
        "properties": {
          "code": {
            "type": "boolean",
            "description": "Indicates the success or failure of the request."
          },
          "description": {
            "type": "string",
            "description": "Additional context about the request to help with debugging."
          },
          "data": {
            "type": "object"
          },
          "error": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "description": "Any errors that occured during the execution of the request."
          }
        },
        "description": "API_Errors_BadRequest model"
      },
      "API_Errors_Unauthorized": {
        "type": "object",
        "properties": {
          "code": {
            "type": "boolean",
            "description": "Indicates the success or failure of the request."
          },
          "description": {
            "type": "string",
            "description": "Additional context about the request to help with debugging."
          },
          "data": {
            "type": "object"
          },
          "error": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "description": "Any errors that occured during the execution of the request."
          }
        },
        "description": "API_Errors_Unauthorized model"
      },
      "API_Errors_Forbidden": {
        "type": "object",
        "properties": {
          "code": {
            "type": "boolean",
            "description": "Indicates the success or failure of the request."
          },
          "description": {
            "type": "string",
            "description": "Additional context about the request to help with debugging."
          },
          "data": {
            "type": "object"
          },
          "error": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "description": "Any errors that occured during the execution of the request."
          }
        },
        "description": "API_Errors_Forbidden model"
      },
      "API_Errors_InternalServerError": {
        "type": "object",
        "properties": {
          "code": {
            "type": "boolean",
            "description": "Indicates the success or failure of the request."
          },
          "description": {
            "type": "string",
            "description": "Additional context about the request to help with debugging."
          },
          "data": {
            "type": "object"
          },
          "error": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "description": "Any errors that occured during the execution of the request."
          }
        },
        "description": "API_Errors_InternalServerError model"
      },
      "API_Errors_NotFound": {
        "type": "object",
        "properties": {
          "code": {
            "type": "boolean",
            "description": "Indicates the success or failure of the request."
          },
          "description": {
            "type": "string",
            "description": "Additional context about the request to help with debugging."
          },
          "data": {
            "type": "object"
          },
          "error": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "description": "Any errors that occured during the execution of the request."
          }
        },
        "description": "API_Errors_NotFound model"
      },
      "postPatients": {
        "type": "object",
        "properties": {
          "provider": {
            "type": "object",
            "example": {
              "provider_id": 12
            },
            "properties": {
              "provider_id": {
                "type": "integer",
                "format": "int64",
                "description": "Id of the provider with which to intake this new patient"
              }
            },
            "required": [
              "provider_id"
            ]
          },
          "patient": {
            "type": "object",
            "properties": {
              "first_name": {
                "type": "string",
                "description": "Patient first name"
              },
              "last_name": {
                "type": "string",
                "description": "Patient last name"
              },
              "email": {
                "type": "string",
                "description": "Patient email id. Must match regular expression /\\A([^@\\s]+)@((?:[-a-z0-9]\\.)[a-z]{2,})\\Z/"
              },
              "bio": {
                "type": "object",
                "description": "Patient Bio",
                "properties": {
                  "date_of_birth": {
                    "type": "string",
                    "format": "date",
                    "description": "Patient date of birth. Must be a parseable date string, recommended formats is YYYY-MM-DD"
                  },
                  "phone_number": {
                    "type": "string",
                    "description": "Patient phone number"
                  },
                  "home_phone_number": {
                    "type": "string",
                    "description": "Patient home phone number"
                  },
                  "cell_phone_number": {
                    "type": "string",
                    "description": "Patient cell phone numnber"
                  },
                  "work_phone_number": {
                    "type": "string",
                    "description": "Patient work place phone number"
                  },
                  "custom_contact_number": {
                    "type": "string",
                    "description": "Patient custom contact phone number"
                  },
                  "gender": {
                    "type": "string",
                    "description": "Patient gender. Gender will be  default to Female if not provided. This is to ensure compatibility with all EHRs. This will be updated when the patient visits the office and updates their info",
                    "enum": [
                      "Male",
                      "Female",
                      "Other"
                    ]
                  },
                  "weight": {
                    "type": "integer",
                    "format": "int32",
                    "description": "Patient weight in KG"
                  },
                  "height": {
                    "type": "integer",
                    "format": "int32",
                    "description": "Patient height in CM"
                  },
                  "street_address": {
                    "type": "string",
                    "description": "Patient full street address"
                  },
                  "address_line_1": {
                    "type": "string",
                    "description": "Patient street address line 1"
                  },
                  "address_line_2": {
                    "type": "string",
                    "description": "Patient street address line 2"
                  },
                  "city": {
                    "type": "string",
                    "description": "Patient city"
                  },
                  "state": {
                    "type": "string",
                    "description": "Patient living state"
                  },
                  "zip_code": {
                    "type": "string",
                    "description": "Patient zip code"
                  },
                  "insurance_name": {
                    "type": "string",
                    "description": "Insurance name"
                  },
                  "ssn": {
                    "type": "string",
                    "description": "Patient SSN"
                  },
                  "race": {
                    "type": "string",
                    "description": "Patient race"
                  }
                },
                "required": [
                  "date_of_birth",
                  "phone_number"
                ]
              }
            },
            "required": [
              "first_name",
              "last_name",
              "email",
              "bio"
            ]
          },
          "return_existing_if_match": {
            "type": "boolean",
            "description": "If true, return existing patient with matching information (200 OK) instead of raising an error. If false, raise an error when a patient with matching information already exists (400 Bad Request). Matching is based on date of birth, name, and phone number.",
            "default": false
          }
        },
        "required": [
          "provider",
          "patient"
        ],
        "description": "Create patient"
      },
      "API_V2_Entities_PatientsResponses_Create_Response": {
        "type": "object",
        "properties": {
          "code": {
            "type": "boolean",
            "example": false,
            "description": "Indicates the success or failure of the request"
          },
          "description": {
            "type": "string",
            "example": "Description",
            "description": "Additional context on the request to help with debugging."
          },
          "error": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "example": "Error",
            "description": "Any errors that occur during the execution of the request."
          },
          "data": {
            "$ref": "#/components/schemas/API_V2_Entities_PatientsResponses_Create"
          },
          "count": {
            "type": "integer",
            "format": "int32",
            "example": 2,
            "description": "Number of total objects, in case of collection."
          }
        },
        "description": "API_V2_Entities_PatientsResponses_Create_Response model"
      },
      "API_V2_Entities_PatientsResponses_Create": {
        "type": "object",
        "properties": {
          "user": {
            "$ref": "#/components/schemas/API_V2_Entities_PatientBasic"
          }
        }
      }
    }
  }
}
```