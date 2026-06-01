REPORT zsignatures.

FORM single_line_form USING p_a TYPE i p_b TYPE string.
ENDFORM.

FORM multi_line_form USING p_a TYPE i
                           p_b TYPE string
                     CHANGING p_c TYPE i.
ENDFORM.

FORM no_params.
ENDFORM.
