REPORT z_booking_report.

INCLUDE z_booking_top.

START-OF-SELECTION.
  PERFORM main_routine.
  CALL FUNCTION 'BAPI_TRANSACTION_COMMIT'.

FORM main_routine.
  PERFORM helper_routine.
ENDFORM.

FORM helper_routine.
ENDFORM.
