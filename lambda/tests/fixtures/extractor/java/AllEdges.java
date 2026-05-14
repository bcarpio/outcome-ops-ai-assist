package com.hyatt.booking;

import com.hyatt.util.Logger;
import com.hyatt.util.Helper;
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
