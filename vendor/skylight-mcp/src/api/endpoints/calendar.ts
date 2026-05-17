import { getClient } from "../client.js";
import type {
  CalendarEventsResponse,
  CalendarEventResource,
  CalendarEventResponse,
  SourceCalendarsResponse,
  SourceCalendarResource,
  CreateCalendarEventRequest,
  UpdateCalendarEventRequest,
} from "../types.js";

export interface GetCalendarEventsOptions {
  dateMin: string;
  dateMax: string;
  timezone?: string;
  include?: string;
}

/**
 * Add days to a date string in YYYY-MM-DD format
 */
function addDays(dateStr: string, days: number): string {
  const date = new Date(dateStr + "T00:00:00");
  date.setDate(date.getDate() + days);
  return date.toISOString().split("T")[0];
}

/**
 * Get calendar events for a date range
 * Note: The API treats date_max as exclusive, so we add 1 day to include events on the end date
 */
export async function getCalendarEvents(
  options: GetCalendarEventsOptions
): Promise<CalendarEventResource[]> {
  const client = getClient();

  // API treats date_max as exclusive, so add 1 day to include events on the end date
  const adjustedDateMax = addDays(options.dateMax, 1);

  const response = await client.get<CalendarEventsResponse>(
    "/api/frames/{frameId}/calendar_events",
    {
      date_min: options.dateMin,
      date_max: adjustedDateMax,
      timezone: options.timezone ?? client.timezone,
      include: options.include,
    }
  );
  return response.data;
}

/**
 * Get source calendars (connected calendar accounts)
 */
export async function getSourceCalendars(): Promise<SourceCalendarResource[]> {
  const client = getClient();
  const response = await client.get<SourceCalendarsResponse>(
    "/api/frames/{frameId}/source_calendars"
  );
  return response.data;
}

/**
 * Create a calendar event
 */
export async function createCalendarEvent(
  data: CreateCalendarEventRequest
): Promise<CalendarEventResource> {
  const client = getClient();
  const response = await client.post<CalendarEventResponse>(
    "/api/frames/{frameId}/calendar_events",
    data
  );
  return response.data;
}

/**
 * Update a calendar event
 */
export async function updateCalendarEvent(
  eventId: string,
  data: UpdateCalendarEventRequest
): Promise<CalendarEventResource> {
  const client = getClient();
  const response = await client.request<CalendarEventResponse>(
    `/api/frames/{frameId}/calendar_events/${eventId}`,
    { method: "PUT", body: data }
  );
  return response.data;
}

/**
 * Delete a calendar event
 */
export async function deleteCalendarEvent(eventId: string): Promise<void> {
  const client = getClient();
  await client.request(`/api/frames/{frameId}/calendar_events/${eventId}`, {
    method: "DELETE",
  });
}
