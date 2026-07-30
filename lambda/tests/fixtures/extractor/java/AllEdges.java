package com.acme.booking;

import com.acme.util.Logger;
import com.acme.util.Helper;
import java.util.List;

public class BookingService extends BaseService implements IBookable, Reportable {

    public void confirm() {
        Logger.log("hello");
        Helper.normalize("x");
        this.notifyOps();
    }

    private void notifyOps() {
        Logger.log("notified");
    }
}
