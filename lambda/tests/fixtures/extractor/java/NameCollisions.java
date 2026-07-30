package com.acme.collisions;

public class NameCollisions {

    public void helper() {
        System.out.println("A");
    }

    public void caller() {
        helper();
    }
}
