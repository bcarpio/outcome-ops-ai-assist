package com.example;

public class Signatures {

    public String greet(String name, boolean loud) {
        return name;
    }

    public void noReturn(int a) {
        // void method
    }

    public int withVarargs(String prefix, String... rest) {
        return rest.length;
    }
}
