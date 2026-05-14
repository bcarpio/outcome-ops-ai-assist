CLASS ZCL_ALL_EDGES DEFINITION
  PUBLIC
  FINAL
  CREATE PUBLIC.

  PUBLIC SECTION.
    METHODS RUN.
    METHODS LOAD.

  PRIVATE SECTION.
    METHODS HELPER.

ENDCLASS.


CLASS ZCL_ALL_EDGES IMPLEMENTATION.

  METHOD RUN.
    DATA: lo_other TYPE REF TO zcl_other.
    INCLUDE zinc_runtime.
    CREATE OBJECT lo_other.
    lo_other->do_work( ).
    zcl_static=>compute( ).
    CALL METHOD zcl_legacy=>log.
    CALL FUNCTION 'BAPI_COMMIT'.
    PERFORM cleanup.
  ENDMETHOD.

  METHOD LOAD.
    zcl_static=>compute( ).
  ENDMETHOD.

  METHOD HELPER.
  ENDMETHOD.

ENDCLASS.
