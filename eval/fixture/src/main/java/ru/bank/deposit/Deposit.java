package ru.bank.deposit;

import javax.persistence.Entity;
import javax.persistence.Table;

@Entity
@Table(name = "m_deposit")
public class Deposit {
    private Long id;
    private Long amount;
}
